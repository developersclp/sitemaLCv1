"""
Servidor Web e Interface SPA do sitemaLCv1 (app.py)
Desenvolvido para Locar Saneamento Ambiental.

Integração completa 100% real com a API Inlog (Inlog.Integration.Coleta.Api):
1. Cruzamento espacial real GPS x Malha Viária (sem dados simulados/aleatórios).
2. Todos os 10 endpoints do manual Inlog disponíveis em camadas no mapa e na Central Inlog.
3. Monitoramento multi-cidades em tempo real e Modo de Edição GIS (QGIS Web) no SQLite.
4. Preservação integral de filtros multi-setor, vias prioritárias (Azul Safira), CSV e links Read-Only.
"""

import base64
import gzip
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional

import database as db
import malha_rotas
import snapshots
from inlog_service import InlogService, formatar_placa_hifen, identificar_contrato
from engine import HeatmapEngine

inlog = InlogService()
engine = HeatmapEngine(inlog_service=inlog)

SECRET_KEY = os.getenv("SECRET_KEY", "locar-saneamento-inlog-secret-key-2026").encode("utf-8")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "filialsaopaulo01")
REQUIRE_ADMIN_LOGIN = os.getenv("REQUIRE_ADMIN_LOGIN", "true").lower() in ("1", "true", "sim", "yes")
SERVER_BOOT_ID = secrets.token_hex(6)


def _admin_pwd_tag() -> str:
    return hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()[:8]


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def gerar_token_compartilhamento(payload: Dict[str, Any]) -> str:
    """Assina criptograficamente um link exclusivo de visualização (HMAC-SHA256)."""
    periodo = str(payload.get("periodo") or "diario").lower().strip()
    if periodo not in ("diario", "quinzena1", "quinzena2", "mensal"):
        periodo = "diario"
    dados = {
        "cidade": str(payload.get("cidade") or "cabo").lower().strip(),
        "data": str(payload.get("data") or "").strip(),
        "periodo": periodo,
        "turno": str(payload.get("turno") or "all").strip(),
        "coleta": str(payload.get("coleta") or "all").strip(),
        "setores": [str(s).strip() for s in (payload.get("setores") or []) if str(s).strip()],
        "v": 3,
    }
    raw = json.dumps(dados, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64_p = _b64u_encode(raw)
    sig = _b64u_encode(hmac.new(SECRET_KEY, b64_p.encode("ascii"), hashlib.sha256).digest())
    return f"{b64_p}.{sig}"


def validar_token_compartilhamento(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Valida a assinatura HMAC-SHA256 e retorna o payload restrito à cidade única."""
    if not token or "." not in token:
        return None
    try:
        b64_p, sig = token.strip().split(".", 1)
        expected = _b64u_encode(hmac.new(SECRET_KEY, b64_p.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        dados = json.loads(_b64u_decode(b64_p).decode("utf-8"))
        if not isinstance(dados, dict) or not dados.get("cidade"):
            return None
        return dados
    except Exception:
        return None


def gerar_cookie_admin() -> str:
    ts = str(int(time.time()))
    tag = _admin_pwd_tag()
    payload = f"admin:{SERVER_BOOT_ID}:{tag}:{ts}"
    sig = _b64u_encode(hmac.new(SECRET_KEY, payload.encode("utf-8"), hashlib.sha256).digest())
    return f"admin.{SERVER_BOOT_ID}.{tag}.{ts}.{sig}"


def validar_cookie_admin(val: Optional[str]) -> bool:
    if not val or val.count(".") != 4:
        return False
    try:
        prefix, boot_id, tag, ts, sig = val.strip().split(".", 4)
        if prefix != "admin" or boot_id != SERVER_BOOT_ID or tag != _admin_pwd_tag():
            return False
        if (time.time() - int(ts)) > 43200:
            return False
        payload = f"admin:{boot_id}:{tag}:{ts}"
        expected = _b64u_encode(hmac.new(SECRET_KEY, payload.encode("utf-8"), hashlib.sha256).digest())
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>LOCAR - Acesso ao Painel Central</title>
    <style>
        body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: #F1F5F9; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; color:#1E293B; }
        .card { background:#FFF; border-radius:12px; box-shadow:0 12px 32px rgba(15,23,42,0.12); padding:28px 32px; width:100%; max-width:410px; border-top:4px solid #008844; }
        .brand { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
        .logo-img { height:44px; width:auto; object-fit:contain; display:block; }
        h1 { font-size:1.1rem; margin:0; }
        .alert { background:#FEF3C7; border:1px solid #FDE68A; color:#92400E; padding:10px 12px; border-radius:8px; font-size:0.78rem; line-height:1.45; margin-bottom:16px; }
        label { display:block; font-size:0.76rem; font-weight:700; color:#475569; margin-bottom:6px; text-transform:uppercase; }
        input { width:100%; padding:10px 12px; border:1px solid #CBD5E1; border-radius:8px; font-size:0.92rem; box-sizing:border-box; margin-bottom:14px; }
        input:focus { outline:none; border-color:#008844; }
        button { width:100%; background:#008844; color:#FFF; border:none; border-radius:8px; padding:11px; font-weight:700; font-size:0.9rem; cursor:pointer; }
        button:hover { background:#007338; }
        .back-btn { display:block; text-align:center; margin-top:12px; padding:9px; border-radius:8px; background:#EFF6FF; color:#1D4ED8; text-decoration:none; font-weight:700; font-size:0.82rem; border:1px solid #BFDBFE; }
        .err { color:#DC2626; font-size:0.8rem; margin-bottom:10px; display:none; font-weight:600; }
    </style>
</head>
<body>
    <div class="card">
        <div class="brand">
            <img src="/static/logo-locar.png" alt="LOCAR Gestão de Resíduos" class="logo-img" />
            <div><h1>Painel Central Operacional</h1></div>
        </div>
        __LOCKED_NOTICE__
        <form id="loginForm">
            <label for="pwd">Chave de Acesso Administrativo</label>
            <input type="password" id="pwd" placeholder="Digite a senha do setor..." autofocus required />
            <div id="errBox" class="err">Chave de acesso inválida.</div>
            <button type="submit">🔓 Desbloquear Painel Central</button>
        </form>
        __BACK_TO_LINK__
    </div>
    <script>
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const password = document.getElementById('pwd').value;
            const resp = await fetch('/api/auth/login', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ password })
            });
            const res = await resp.json();
            if (res.sucesso) window.location.href = res.redirect || '/';
            else document.getElementById('errBox').style.display = 'block';
        });
    </script>
</body>
</html>"""

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="utf-8" />
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
    <meta http-equiv="Pragma" content="no-cache" />
    <meta http-equiv="Expires" content="0" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>LOCAR - Mapa de Execução · Inlog, Auditoria & GIS</title>
    <!-- __SHARED_LOCK_INIT__ -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script defer src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">

    <style>
        :root {
            --locar-green: #008844;
            --locar-green-dark: #005C2E;
            --locar-green-light: #E8F5E9;
            --locar-green-hover: #007338;
            --edit-amber: #D97706;
            --edit-amber-dark: #92400E;
            --priority-blue: #1D4ED8;
            --inlog-indigo: #4338CA;
            --gray-bg: #F8FAFC;
            --gray-border: #E2E8F0;
            --text-dark: #1E293B;
            --text-muted: #64748B;
            --danger-red: #E53935;
            --warning-amber: #FBC02D;
            --manual-orange: #0284C7;
            --collected-green: #00A859;
            --card-shadow: 0 2px 4px rgba(0, 0, 0, 0.04), 0 1px 2px rgba(0, 0, 0, 0.03);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--gray-bg);
            color: var(--text-dark);
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        /* HEADER */
        header {
            background-color: #FFFFFF;
            border-bottom: 2px solid var(--locar-green);
            padding: 4px 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            z-index: 1000;
            gap: 8px;
            height: 42px;
            min-height: 42px;
            flex-shrink: 0;
            overflow-x: hidden;
            width: 100%;
        }
        .header-brand { display: flex; align-items: center; gap: 8px; flex-shrink: 0; min-width: 0; }
        .logo-box {
            background: linear-gradient(135deg, var(--locar-green), var(--locar-green-dark));
            color: white;
            font-weight: 800;
            font-size: 0.88rem;
            letter-spacing: 0.5px;
            padding: 2px 7px;
            border-radius: 4px;
            flex-shrink: 0;
        }
        .header-title { display: flex; align-items: baseline; gap: 6px; min-width: 0; overflow: hidden; }
        .header-title h1 { font-size: 0.88rem; font-weight: 700; color: var(--text-dark); white-space: nowrap; margin: 0; }
        .header-title .tagline { font-size: 0.72rem; color: var(--text-muted); font-weight: 400; white-space: nowrap; }
        @media (max-width: 1100px) {
            .header-title .tagline { display: none; }
        }
        .header-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
        .badge-status {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 7px;
            border-radius: 9999px;
            font-size: 0.66rem;
            font-weight: 700;
            background-color: var(--locar-green-light);
            color: var(--locar-green-dark);
            white-space: nowrap;
            flex-shrink: 0;
        }
        .badge-status.offline { background-color: #FEE2E2; color: #991B1B; }
        .badge-dot { width: 6px; height: 6px; border-radius: 50%; background-color: var(--locar-green); flex-shrink: 0; }
        .badge-status.offline .badge-dot { background-color: #DC2626; }

        /* TOOLBAR */
        .toolbar {
            background: #FFFFFF;
            border-bottom: 1px solid var(--gray-border);
            padding: 3px 12px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 4px 8px;
            z-index: 999;
            min-height: 38px;
            flex-shrink: 0;
            overflow-x: hidden;
            width: 100%;
        }
        .toolbar-left {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 4px 6px;
            flex: 1 1 auto;
            min-width: 0;
        }
        .toolbar-right {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 4px 5px;
            flex-shrink: 0;
            margin-left: auto;
        }
        .filter-group { display: flex; align-items: center; gap: 3px; flex-shrink: 0; }
        .filter-group label {
            font-size: 0.66rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            white-space: nowrap;
        }
        select, input[type="date"], input[type="text"] {
            border: 1px solid var(--gray-border);
            border-radius: 5px;
            padding: 2px 6px;
            font-size: 0.75rem;
            font-family: inherit;
            color: var(--text-dark);
            background-color: #FFFFFF;
            outline: none;
            height: 27px;
            transition: border-color 0.15s;
        }
        .toolbar select, .toolbar input[type="date"], .toolbar input[type="text"] {
            max-width: 175px;
        }
        select:focus, input:focus { border-color: var(--locar-green); }

        .btn-locar {
            background-color: var(--locar-green);
            color: #FFFFFF;
            border: none;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 0.74rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            height: 27px;
            transition: all 0.15s;
            white-space: nowrap;
            flex-shrink: 0;
        }
        .btn-locar:hover { background-color: var(--locar-green-hover); }

        .btn-inlog {
            background-color: #EEF2FF;
            color: var(--inlog-indigo);
            border: 1px solid #C7D2FE;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 0.74rem;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            height: 27px;
            transition: all 0.15s;
            white-space: nowrap;
            flex-shrink: 0;
        }
        .btn-inlog:hover { background-color: #E0E7FF; }

        .btn-outline {
            background-color: transparent;
            color: var(--text-dark);
            border: 1px solid var(--gray-border);
            border-radius: 5px;
            padding: 2px 7px;
            font-size: 0.74rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            height: 27px;
            transition: all 0.15s;
            white-space: nowrap;
            flex-shrink: 0;
        }
        .btn-outline:hover { background-color: var(--gray-bg); border-color: #CBD5E1; }

        .btn-edit-mode {
            background-color: #FEF3C7;
            color: var(--edit-amber-dark);
            border: 1px solid #FCD34D;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 0.74rem;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            height: 27px;
            transition: all 0.15s;
            white-space: nowrap;
            flex-shrink: 0;
        }
        .btn-edit-mode:hover { background-color: #FDE68A; }
        .btn-edit-mode.active {
            background-color: var(--edit-amber);
            color: #FFFFFF;
            border-color: var(--edit-amber-dark);
            box-shadow: 0 0 0 2px rgba(217, 119, 6, 0.3);
        }

        .frequencia-pill-box {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: #ECFDF5;
            border: 1px solid #A7F3D0;
            padding: 2px 7px;
            border-radius: 5px;
            font-size: 0.70rem;
            font-weight: 700;
            color: #065F46;
            height: 27px;
            white-space: nowrap;
            cursor: default;
            flex-shrink: 0;
        }

        /* STATS BAR */
        .stats-bar {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 8px;
            padding: 5px 16px;
            background-color: #FAFAFA;
            border-bottom: 1px solid var(--gray-border);
            flex-shrink: 0;
        }
        .stat-card {
            background: #FFFFFF;
            border: 1px solid var(--gray-border);
            border-radius: 5px;
            padding: 4px 10px;
            display: flex;
            flex-direction: column;
            box-shadow: var(--card-shadow);
        }
        .stat-card.destaque { border-left: 4px solid var(--locar-green); }
        .stat-card.manual { border-left: 4px solid var(--manual-orange); }
        .stat-card.alerta { border-left: 4px solid var(--danger-red); }
        .stat-card.inlog { border-left: 4px solid var(--inlog-indigo); }

        .map-print-btn {
            position: absolute;
            bottom: 18px;
            left: 14px;
            z-index: 820;
            background: #0F172A;
            color: #FFF;
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 8px;
            padding: 8px 13px;
            font-size: 0.78rem;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.25);
            display: none;
            align-items: center;
            gap: 6px;
        }
        .map-print-btn:hover { background: var(--locar-green); }
        .all-cities-legend {
            position: absolute;
            bottom: 16px;
            left: 14px;
            z-index: 810;
            background: rgba(255, 255, 255, 0.22);
            backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.45);
            border-radius: 10px;
            padding: 7px 10px;
            box-shadow: 0 4px 16px rgba(15, 23, 42, 0.12);
            max-width: min(560px, calc(100% - 28px));
            display: none;
            flex-wrap: wrap;
            align-items: center;
            justify-content: flex-start;
            gap: 5px;
        }
        .city-pill {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 4px 8px;
            border-radius: 999px;
            font-size: 0.71rem;
            font-weight: 700;
            border: 1px solid rgba(255, 255, 255, 0.65);
            background: rgba(255, 255, 255, 0.72);
            backdrop-filter: blur(6px);
            color: #1E293B;
            cursor: pointer;
            white-space: nowrap;
        }
        .city-pill:hover { border-color: var(--locar-green); background: #F0FDF4; }
        .city-pill .pct-ok { color: #008844; }
        .city-pill .pct-mid { color: #D97706; }
        .city-pill .pct-low { color: #DC2626; }

        /* MODO LINK COMPARTILHADO (RESTRITO A UMA CIDADE E SOMENTE VISUALIZAÇÃO) */
        body.modo-link-compartilhado .toolbar { display: none !important; }
        body.modo-link-compartilhado #inlogStatusBadge { display: none !important; }
        body.modo-link-compartilhado #btnPlanejadoVsExec,
        body.modo-link-compartilhado #btnVisaoGeral,
        body.modo-link-compartilhado #btnCentralInlog,
        body.modo-link-compartilhado #btnSairAdmin { display: none !important; }
        body.modo-link-compartilhado #btnBaixarCsvLink { display: inline-flex !important; }
        body.modo-link-compartilhado #secCamadasInlog { display: none !important; }
        body.modo-link-compartilhado #gisToolbar,
        body.modo-link-compartilhado #panelEdicaoGIS { display: none !important; }

        /* BOTÃO E CABEÇALHO MOBILE (OCULTOS NO DESKTOP) */
        .mobile-panel-toggle {
            display: none;
            background: #008844;
            color: #FFFFFF;
            border: none;
            border-radius: 5px;
            padding: 4px 8px;
            font-size: 0.74rem;
            font-weight: 700;
            cursor: pointer;
            align-items: center;
            gap: 4px;
            height: 28px;
        }
        .sidebar-mobile-header {
            display: none;
        }
        .stat-title {
            font-size: 0.63rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.2px;
        }
        .stat-value {
            font-size: 1.0rem;
            font-weight: 700;
            color: var(--text-dark);
            margin-top: 1px;
            line-height: 1.2;
        }
        .stat-sub { font-size: 0.68rem; color: var(--text-muted); }

        /* MAIN AREA */
        .main-container {
            flex: 1;
            display: flex;
            position: relative;
            overflow: hidden;
        }
        #map { flex: 1; height: 100%; }

        /* BANNER CIDADE SEM MALHA */
        #noMeshBanner {
            position: absolute;
            bottom: 16px;
            left: 16px;
            max-width: 480px;
            background: rgba(15, 23, 42, 0.92);
            color: #F8FAFC;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 0.76rem;
            line-height: 1.4;
            z-index: 950;
            display: none;
            box-shadow: 0 8px 20px rgba(0,0,0,0.25);
            border-left: 4px solid #38BDF8;
        }

        /* BARRA DE INSTRUÇÃO DE DESENHO FLUTUANTE */
        #drawInstructionBox {
            position: absolute;
            top: 14px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(15, 23, 42, 0.95);
            backdrop-filter: blur(4px);
            color: #FFFFFF;
            padding: 8px 18px;
            border-radius: 30px;
            font-size: 0.80rem;
            font-weight: 600;
            box-shadow: 0 6px 18px rgba(0,0,0,0.25);
            z-index: 1000;
            display: none;
            align-items: center;
            gap: 12px;
            border: 1px solid rgba(255,255,255,0.2);
        }
        #drawInstructionBox button {
            background: #E2E8F0;
            border: none;
            border-radius: 14px;
            padding: 3px 10px;
            font-size: 0.74rem;
            font-weight: 700;
            cursor: pointer;
            color: #0F172A;
        }

        /* TOOLBAR GIS FLUTUANTE ESTILO QGIS */
        #gisToolbar {
            position: absolute;
            top: 14px;
            left: 56px;
            background: #FFFFFF;
            border: 1px solid var(--gray-border);
            border-radius: 8px;
            padding: 5px;
            box-shadow: 0 6px 16px rgba(0,0,0,0.12);
            z-index: 999;
            display: none;
            gap: 6px;
            flex-wrap: wrap;
        }
        .gis-tool-btn {
            background: #F1F5F9;
            border: 1px solid #CBD5E1;
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 0.76rem;
            font-weight: 600;
            color: var(--text-dark);
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            transition: all 0.15s;
        }
        .gis-tool-btn:hover { background: #E2E8F0; }
        .gis-tool-btn.active {
            background: var(--edit-amber);
            color: white;
            border-color: var(--edit-amber-dark);
        }

        /* SIDEBAR DIREITA */
        .sidebar {
            width: 365px;
            background-color: #FFFFFF;
            border-left: 1px solid var(--gray-border);
            display: flex;
            flex-direction: column;
            z-index: 500;
            box-shadow: -2px 0 6px rgba(0,0,0,0.03);
            overflow-y: auto;
        }
        .sidebar-section {
            padding: 12px 15px;
            border-bottom: 1px solid var(--gray-border);
        }
        .sidebar-section h4 {
            font-size: 0.73rem;
            font-weight: 800;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.6px;
            margin-bottom: 10px;
        }
        .sidebar-field { margin-bottom: 10px; }
        .sidebar-field:last-child { margin-bottom: 0; }
        .sidebar-field label {
            display: block;
            font-size: 0.76rem;
            font-weight: 600;
            color: var(--text-dark);
            margin-bottom: 4px;
        }
        .sidebar-field select, .sidebar-field input[type="text"], .sidebar-field input[type="date"] {
            width: 100%;
            padding: 6px 10px;
            font-size: 0.80rem;
        }
        .section-hint {
            font-size: 0.70rem;
            color: var(--text-muted);
            font-style: italic;
            margin-top: 4px;
        }

        .toggle-group {
            display: flex;
            border: 1px solid var(--gray-border);
            border-radius: 6px;
            overflow: hidden;
            margin-top: 4px;
        }
        .toggle-btn {
            flex: 1;
            padding: 5px 10px;
            font-size: 0.76rem;
            font-weight: 700;
            border: none;
            cursor: pointer;
            background: #FFFFFF;
            color: var(--text-muted);
            transition: all 0.15s;
        }
        .toggle-btn.active { background: #1E293B; color: #FFFFFF; }

        .layer-check-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 8px;
            background: #F8FAFC;
            border: 1px solid var(--gray-border);
            border-radius: 6px;
            margin-bottom: 6px;
            font-size: 0.77rem;
            font-weight: 600;
            cursor: pointer;
        }
        .layer-check-row:hover { border-color: #CBD5E1; background: #F1F5F9; }
        .layer-check-left { display: flex; align-items: center; gap: 8px; }
        .layer-check-row input[type="checkbox"] { width: 15px; height: 15px; accent-color: var(--locar-green); cursor: pointer; }
        .layer-pill {
            font-size: 0.68rem;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 10px;
            background: #E2E8F0;
            color: #334155;
        }

        .legend-compact { display: flex; flex-direction: column; gap: 5px; }
        .legend-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.77rem; }
        .legend-color-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }

        /* RÓTULOS DOS SETORES */
        .sector-badge-container { position: relative; cursor: pointer; pointer-events: auto !important; }
        .sector-dot-marker {
            width: 10px; height: 10px; border-radius: 50%;
            background-color: var(--locar-green-dark);
            border: 2px solid #FFFFFF;
            box-shadow: 0 1px 4px rgba(0,0,0,0.3);
            opacity: 1;
            transition: all 0.2s ease;
        }
        .sector-pill-label {
            position: absolute; bottom: 14px; left: 50%;
            transform: translateX(-50%) scale(1);
            background: rgba(15, 23, 42, 0.92);
            color: #FFFFFF; font-size: 11px; font-weight: 700; letter-spacing: 0.4px;
            padding: 3px 9px; border-radius: 9999px; white-space: nowrap;
            box-shadow: 0 4px 12px rgba(0,0,0,0.25);
            opacity: 1; pointer-events: none; transition: all 0.2s;
            border: 1px solid rgba(255,255,255,0.15);
        }
        .sector-badge-container:hover .sector-dot-marker { opacity: 1; transform: scale(1.3); background-color: var(--locar-green); }
        .sector-badge-container:hover .sector-pill-label { opacity: 1; transform: translateX(-50%) scale(1); }
        .sector-badge-container.destaque .sector-dot-marker { opacity: 1; background-color: var(--locar-green); transform: scale(1.3); }
        .sector-badge-container.destaque .sector-pill-label { opacity: 1; transform: translateX(-50%) scale(1); background: var(--locar-green-dark); border-color: #6EE7B7; }

        /* MARCADORES DE VEÍCULOS INLOG */
        .truck-marker-box {
            display: flex;
            flex-direction: column;
            align-items: center;
            transform: translate(-50%, -50%);
        }
        .truck-dot {
            width: 22px; height: 22px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            color: #FFFFFF; font-size: 11px; font-weight: 800;
            border: 2px solid #FFFFFF;
            box-shadow: 0 2px 6px rgba(0,0,0,0.35);
        }
        .truck-dot.on { background: #16A34A; }
        .truck-dot.moving { background: #2563EB; }
        .truck-dot.off { background: #64748B; }
        .truck-plate-tag {
            margin-top: 1px;
            background: rgba(15, 23, 42, 0.88);
            color: #FFFFFF;
            font-size: 9.5px;
            font-weight: 700;
            padding: 1px 5px;
            border-radius: 4px;
            white-space: nowrap;
            letter-spacing: 0.3px;
        }

        /* SPINNER */
        #loadingOverlay {
            position: absolute; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(255, 255, 255, 0.78);
            backdrop-filter: blur(2px);
            z-index: 2000;
            display: none;
            align-items: center; justify-content: center; flex-direction: column; gap: 10px;
        }
        .spinner {
            width: 38px; height: 38px;
            border: 4px solid var(--gray-border);
            border-top-color: var(--locar-green);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text { font-size: 0.84rem; font-weight: 600; color: var(--locar-green-dark); }

        .badge-readonly {
            background: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE;
            border-radius: 6px; padding: 6px 11px; font-size: 0.77rem; font-weight: 700;
            display: inline-flex; align-items: center; gap: 6px;
        }

        /* MULTI-SELECT DE SETORES */
        .multi-select-container { position: relative; width: 100%; }
        .multi-select-trigger {
            width: 100%; display: flex; justify-content: space-between; align-items: center;
            padding: 6px 10px; background: #FFFFFF; border: 1px solid var(--gray-border);
            border-radius: 6px; font-size: 0.80rem; font-family: inherit;
            color: var(--text-dark); cursor: pointer; text-align: left;
        }
        .multi-select-trigger:hover { border-color: var(--locar-green); }
        .multi-select-trigger.active { border-color: var(--locar-green); box-shadow: 0 0 0 2px rgba(0, 136, 68, 0.15); }
        .trigger-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; font-weight: 500; }
        .sector-count-pill { font-size: 0.69rem; font-weight: 700; padding: 2px 7px; border-radius: 12px; background: #E2E8F0; color: var(--text-dark); }
        .sector-count-pill.active { background: var(--locar-green-light); color: var(--locar-green-dark); }
        .multi-select-dropdown {
            position: absolute; top: calc(100% + 4px); left: 0; right: 0;
            background: #FFFFFF; border: 1px solid var(--gray-border); border-radius: 8px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.15); z-index: 1500; overflow: hidden;
        }
        .multi-select-header { padding: 8px; background: #F8FAFC; border-bottom: 1px solid var(--gray-border); display: flex; flex-direction: column; gap: 6px; }
        .multi-select-actions { display: flex; gap: 6px; }
        .btn-chip { background: #FFFFFF; border: 1px solid var(--gray-border); border-radius: 4px; padding: 3px 8px; font-size: 0.72rem; font-weight: 600; cursor: pointer; color: var(--text-dark); }
        .btn-chip:hover { background: var(--gray-bg); border-color: var(--locar-green); }
        .multi-select-search { width: 100%; padding: 5px 8px; font-size: 0.77rem; border: 1px solid var(--gray-border); border-radius: 4px; outline: none; }
        .multi-select-list { max-height: 210px; overflow-y: auto; padding: 4px 0; }
        .multi-select-item { display: flex; align-items: center; gap: 8px; padding: 6px 10px; cursor: pointer; font-size: 0.78rem; user-select: none; }
        .multi-select-item:hover { background: #F1F5F9; }
        .multi-select-item.selected { background: #F0FDF4; }
        .multi-select-item input[type="checkbox"] { width: 14px; height: 14px; cursor: pointer; accent-color: var(--locar-green); margin: 0; }
        .sector-item-info { display: flex; justify-content: space-between; width: 100%; align-items: center; }
        .sector-code { font-weight: 600; color: var(--text-dark); }
        .sector-meta { font-size: 0.69rem; color: var(--text-muted); }

        /* MODAIS */
        .modal-backdrop {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(15, 23, 42, 0.65);
            backdrop-filter: blur(4px);
            z-index: 3000;
            display: flex; align-items: center; justify-content: center; padding: 16px;
        }
        .modal-card {
            background: #FFFFFF; border-radius: 12px; width: 100%; max-width: 560px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.28); overflow: hidden;
            display: flex; flex-direction: column; animation: modalFadeIn 0.2s ease-out;
        }
        .modal-card.modal-xl { max-width: 1180px; height: 88vh; }
        .modal-card.modal-lg { max-width: 960px; max-height: 86vh; }
        @keyframes modalFadeIn { from { opacity: 0; transform: translateY(-10px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
        .modal-header { padding: 14px 20px; background: #F8FAFC; border-bottom: 1px solid var(--gray-border); display: flex; justify-content: space-between; align-items: center; }
        .modal-icon-badge { width: 34px; height: 34px; background: #DCFCE7; color: var(--locar-green-dark); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; }
        .modal-title { font-size: 0.96rem; font-weight: 700; color: var(--text-dark); margin: 0; }
        .modal-subtitle { font-size: 0.73rem; color: var(--text-muted); margin: 2px 0 0 0; }
        .modal-close-btn { background: none; border: none; font-size: 22px; cursor: pointer; color: var(--text-muted); line-height: 1; }
        .modal-close-btn:hover { color: var(--danger-red); }
        .modal-body { padding: 16px 20px; overflow-y: auto; flex: 1; }
        .modal-footer { padding: 11px 20px; background: #F8FAFC; border-top: 1px solid var(--gray-border); display: flex; justify-content: flex-end; gap: 10px; }

        /* TABS & TABELAS CENTRAL INLOG */
        .tabs-bar {
            display: flex; gap: 4px; padding: 10px 20px 0 20px;
            background: #F1F5F9; border-bottom: 1px solid var(--gray-border); overflow-x: auto;
        }
        .tab-btn {
            padding: 8px 14px; border: 1px solid transparent; border-bottom: none;
            background: transparent; border-radius: 8px 8px 0 0; font-size: 0.78rem;
            font-weight: 700; color: var(--text-muted); cursor: pointer; white-space: nowrap;
        }
        .tab-btn:hover { color: var(--text-dark); background: rgba(255,255,255,0.6); }
        .tab-btn.active { background: #FFFFFF; color: var(--locar-green-dark); border-color: var(--gray-border); }

        .tab-toolbar {
            display: flex; justify-content: space-between; align-items: center; gap: 10px;
            flex-wrap: wrap; margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid var(--gray-border);
        }
        .data-table-wrap { overflow: auto; border: 1px solid var(--gray-border); border-radius: 8px; max-height: calc(88vh - 245px); }
        table.inlog-table { width: 100%; border-collapse: collapse; font-size: 0.77rem; }
        table.inlog-table th {
            position: sticky; top: 0; background: #F8FAFC; color: #475569;
            font-weight: 700; text-align: left; padding: 8px 10px;
            border-bottom: 2px solid var(--gray-border); white-space: nowrap; z-index: 2;
        }
        table.inlog-table td { padding: 7px 10px; border-bottom: 1px solid #F1F5F9; vertical-align: middle; }
        table.inlog-table tr:hover td { background: #F8FAFC; }

        .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.69rem; font-weight: 700; }
        .pill-green { background: #DCFCE7; color: #166534; }
        .pill-red { background: #FEE2E2; color: #991B1B; }
        .pill-amber { background: #FEF3C7; color: #92400E; }
        .pill-blue { background: #DBEAFE; color: #1E40AF; }
        .pill-gray { background: #E2E8F0; color: #334155; }

        /* VISÃO GERAL MULTI-CIDADES */
        .overview-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
        .city-overview-card {
            background: #FFFFFF; border: 1px solid var(--gray-border); border-radius: 8px;
            padding: 12px 14px; display: flex; flex-direction: column; gap: 8px;
            box-shadow: var(--card-shadow); transition: border-color 0.15s, transform 0.1s;
        }
        .city-overview-card:hover { border-color: var(--locar-green); transform: translateY(-1px); }
        .city-overview-head { display: flex; justify-content: space-between; align-items: center; }
        .city-overview-name { font-size: 0.88rem; font-weight: 700; color: var(--text-dark); }
        .city-overview-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.74rem; color: var(--text-muted); }
        .city-overview-metrics b { color: var(--text-dark); }

        .security-callout { background: #EFF6FF; border-left: 4px solid #3B82F6; border-radius: 6px; padding: 10px 12px; margin-bottom: 14px; }
        .modal-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .share-sectors-summary { background: #F8FAFC; border: 1px solid var(--gray-border); border-radius: 6px; padding: 8px 10px; font-size: 0.78rem; font-weight: 600; }
        .cities-checkbox-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; background: #F8FAFC; border: 1px solid var(--gray-border); border-radius: 6px; padding: 8px; max-height: 110px; overflow-y: auto; }
        .city-checkbox-item { display: flex; align-items: center; gap: 6px; font-size: 0.75rem; cursor: pointer; }
        .share-input-group { display: flex; gap: 8px; margin-top: 4px; }
        .share-url-input { flex: 1; font-family: monospace; font-size: 0.76rem; background: #F1F5F9; border: 1px solid var(--gray-border); border-radius: 6px; padding: 7px 10px; }

        .locar-toast {
            position: fixed; bottom: 24px; right: 24px; background: #0F172A; color: #FFFFFF;
            padding: 10px 18px; border-radius: 8px; font-size: 0.81rem; font-weight: 600;
            box-shadow: 0 10px 25px rgba(0,0,0,0.3); z-index: 4500; display: none;
            align-items: center; gap: 8px; animation: toastIn 0.25s ease-out;
        }
        @keyframes toastIn { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

        .macros-drawer {
            margin-top: 12px; padding: 12px; background: #F8FAFC; border: 1px solid var(--gray-border); border-radius: 8px;
        }

        /* RESPONSIVIDADE MOBILE E TABLET (SEMPRE NO FINAL PARA SOBRESCREVER DESKTOP) */
        @media (max-width: 768px) {
            body {
                height: 100dvh;
            }
            header {
                height: auto;
                min-height: 40px;
                padding: 5px 10px;
                flex-wrap: wrap;
                gap: 5px;
            }
            .header-brand {
                flex: 1 1 auto;
                min-width: 0;
                gap: 6px;
            }
            .header-title h1 {
                font-size: 0.82rem;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .header-title .tagline {
                display: inline;
                font-size: 0.68rem;
            }
            body:not(.modo-link-compartilhado) .header-title .tagline {
                display: none;
            }
            .header-actions {
                gap: 4px;
                flex-wrap: wrap;
            }
            .mobile-panel-toggle {
                display: inline-flex !important;
            }

            .stats-bar {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 5px;
                padding: 5px 8px;
            }
            .stat-card {
                padding: 4px 7px;
                min-width: 0;
            }
            .stat-title {
                font-size: 0.57rem;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .stat-value {
                font-size: 0.86rem;
                margin-top: 0;
            }
            .stat-sub {
                font-size: 0.59rem;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .main-container {
                flex-direction: column;
                position: relative;
                flex: 1 1 auto;
                min-height: 220px;
            }
            #map {
                width: 100% !important;
                height: 100% !important;
                flex: 1;
            }
            .leaflet-control-attribution {
                margin-bottom: 42px !important;
                font-size: 9px !important;
            }

            .sidebar {
                position: absolute !important;
                bottom: 0;
                left: 0;
                right: 0;
                width: 100% !important;
                max-height: 62vh;
                background: #FFFFFF;
                border-left: none;
                border-top: 2px solid var(--locar-green);
                box-shadow: 0 -6px 20px rgba(15, 23, 42, 0.22);
                transition: transform 0.25s ease-in-out;
                z-index: 1500 !important;
                overflow-y: auto;
                border-radius: 12px 12px 0 0;
            }
            .sidebar.mobile-collapsed {
                transform: translateY(calc(100% - 38px));
                overflow: hidden;
            }
            .sidebar-mobile-header {
                display: flex !important;
                align-items: center;
                justify-content: space-between;
                padding: 9px 14px;
                height: 38px;
                background: #F8FAFC;
                border-bottom: 1px solid var(--gray-border);
                cursor: pointer;
                position: sticky;
                top: 0;
                z-index: 10;
            }
            .sidebar-mobile-header span {
                font-size: 0.76rem;
                font-weight: 700;
                color: var(--text-dark);
            }
            .sidebar-field select, .sidebar-field input[type="text"], .sidebar-field input[type="date"] {
                width: 100% !important;
                max-width: 100% !important;
                height: 34px;
                font-size: 0.82rem;
            }
            .modal-form-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>

    <!-- HEADER -->
    <header>
        <div class="header-brand">
            <img src="/static/logo-locar.png" alt="LOCAR Gestão de Resíduos" style="height:36px; width:auto; object-fit:contain; display:block;" />
            <div class="header-title">
                <h1>SISTEMA LC <span class="tagline" id="headerTagline">· Mapa de Execução Operacional Inlog & GIS</span></h1>
            </div>
            <div class="badge-status" id="inlogStatusBadge" title="Status da Conexão com API Inlog">
                <span class="badge-dot"></span>
                <span id="inlogStatusText">CONECTANDO...</span>
            </div>
        </div>
        <div class="header-actions">
            <button class="mobile-panel-toggle" id="btnToggleSidebarMobile" title="Abrir painel de filtros">
                ☰ Filtros
            </button>
            <button class="btn-outline" id="btnPlanejadoVsExec" title="Comparar Rota Planejada (Inlog) x Rota Executada por cada veículo">
                🧭 Planejado x Real
            </button>
            <button class="btn-outline" id="btnVisaoGeral" title="Monitoramento consolidado de todas as cidades">
                🌎 Cidades
            </button>
            <button class="btn-inlog" id="btnCentralInlog" title="Central de Módulos da API Inlog">
                📊 Central Inlog
            </button>
            <button class="btn-locar" id="btnBaixarCsvLink" style="display:none;" title="Exportar relatório de ruas omitidas em CSV">
                ⬇ Baixar CSV
            </button>
            <a class="btn-outline" id="btnSairAdmin" href="/logout" style="text-decoration:none;" title="Encerrar sessão administrativa">
                🔒 Sair
            </a>
        </div>
    </header>

    <!-- TOOLBAR -->
    <div class="toolbar">
        <div class="toolbar-left">
            <div class="filter-group">
                <label for="selectCidade" title="Cidade / Projeto">Cidade:</label>
                <select id="selectCidade">
                    <option value="all">Todas as Cidades (Visão Geral)</option>
                    <option value="cabo" selected>Cabo de Santo Agostinho</option>
                    <option value="jaboatao">Jaboatão dos Guararapes</option>
                    <option value="olinda">Olinda</option>
                    <option value="paulista">Paulista</option>
                    <option value="caruaru">Caruaru</option>
                    <option value="cuiaba">Cuiabá</option>
                    <option value="garanhuns">Garanhuns</option>
                    <option value="pesqueira">Pesqueira</option>
                    <option value="vitoria">Vitória de Santo Antão</option>
                    <option value="canela">Canela</option>
                    <option value="jequie">Jequié</option>
                    <option value="novo hamburgo">Novo Hamburgo</option>
                </select>
            </div>

            <div class="filter-group">
                <label for="selectOperacao" title="Operação de Coleta">Op:</label>
                <select id="selectOperacao">
                    <option value="COLETA" selected>Coleta (Domiciliar + Manual)</option>
                    <option value="DOMICILIAR">Coleta Domiciliar</option>
                    <option value="MANUAL">Coleta Manual</option>
                </select>
            </div>

            <div class="filter-group" id="groupTurnoTop">
                <label for="selectTurno" title="Turno da Coleta">Turno:</label>
                <select id="selectTurno">
                    <option value="all" selected>Diurno + Noturno</option>
                    <option value="DIURNO">Diurno (D)</option>
                    <option value="NOTURNO">Noturno (N)</option>
                </select>
            </div>

            <div class="filter-group" id="groupDataTurno">
                <label for="inputData">Data:</label>
                <input type="date" id="inputData" />
            </div>

            <div class="frequencia-pill-box" id="pillFrequenciaBox" title="Agenda oficial por sufixo (SEG..DOM, A, E, 1..4/L, QZ) e prefixo (SCD*=Diário, SCP*=Par, SCI*=Ímpar, SCDOM*=Dom)">
                <span id="frequenciaIcon">📅</span>
                <span id="lblFrequenciaRegra">Agenda Oficial</span>
            </div>

            <button class="btn-locar" id="btnConsultar" title="Sincronizar dados com a Inlog e executar auditoria espacial">
                🔄 Sincronizar
            </button>
        </div>

        <div class="toolbar-right">
            <button class="btn-outline" id="btnBaixarCsv" title="Exportar relatório de ruas omitidas em CSV">
                ⬇ CSV
            </button>

            <button class="btn-outline" id="btnShareLink" title="Gerar link direto desta visualização">
                🔗 <span id="textShareLink">Link</span>
            </button>

            <div class="badge-readonly" id="badgeReadOnly" style="display:none;">
                🔒 Somente Leitura
            </div>

            <button class="btn-edit-mode" id="btnToggleEditMode" title="Ativar painel de edição do projeto e malhas">
                ✏ <span id="textEditMode">Edição GIS</span>
            </button>
        </div>
    </div>

    <!-- STATS BAR -->
    <div class="stats-bar">
        <div class="stat-card destaque">
            <span class="stat-title">Cobertura Real do Projeto</span>
            <span class="stat-value" id="kpiCobertura">-- %</span>
            <span class="stat-sub" id="kpiExtensao">-- km coletados</span>
        </div>
        <div class="stat-card manual">
            <span class="stat-title">Coleta Manual</span>
            <span class="stat-value" id="kpiTotalKm" style="color: var(--manual-orange);">-- %</span>
            <span class="stat-sub" id="kpiTotalVias">--% do total foi coleta manual</span>
        </div>
        <div class="stat-card alerta">
            <span class="stat-title">Ruas Omitidas (Vermelhas)</span>
            <span class="stat-value" id="kpiOmitidas" style="color: var(--danger-red);">-- vias</span>
            <span class="stat-sub" id="kpiOmitidasKm">-- km a repassar</span>
        </div>
        <div class="stat-card inlog">
            <span class="stat-title">Frota & Telemetria Inlog</span>
            <span class="stat-value" id="kpiCarros">--</span>
            <span class="stat-sub" id="kpiGpsPontos">Pontos GPS reais: --</span>
        </div>
    </div>

    <!-- MAIN CONTAINER -->
    <div class="main-container">
        <div id="map"></div>
        <button type="button" id="btnPrintMapaCidade" class="map-print-btn" title="Enquadrar cidade, tirar print do mapa e copiar texto para Ctrl+V">
            📸 Print Mapa + Copiar Texto
        </button>
        <div id="allCitiesBottomLegend" class="all-cities-legend"></div>

        <!-- BANNER PARA CIDADES SEM MALHA VIÁRIA IMPORTADA -->
        <div id="noMeshBanner">
            <b>ℹ Malha viária GeoJSON não carregada para esta cidade.</b><br/>
            Frota em tempo real, rastro GPS real da Inlog, setores, RD, alarmes, falhas e telemetria funcionam normalmente. Use <b>Edição do Projeto -> Importar Malha</b> para carregar as ruas.
        </div>

        <!-- BARRA DE INSTRUÇÃO DE DESENHO FLUTUANTE -->
        <div id="drawInstructionBox">
            <span id="drawInstructionText">📍 1º Clique: Marque o INÍCIO da nova rua.</span>
            <button id="btnCancelDrawing">Cancelar</button>
        </div>

        <!-- BARRA FLUTUANTE DE FERRAMENTAS GIS (QGIS) -->
        <div id="gisToolbar">
            <button class="gis-tool-btn active" id="toolSelect">🔍 Selecionar</button>
            <button class="gis-tool-btn" id="toolDrawLine">➕ Desenhar Rua</button>
            <button class="gis-tool-btn" id="toolDelete">🗑 Apagar Rua</button>
            <button class="gis-tool-btn" id="toolExportGeoJson" style="background:#EFF6FF; color:#1D4ED8; border-color:#BFDBFE;">
                📥 Exportar GeoJSON
            </button>
            <button class="gis-tool-btn" id="toolImportGeoJson" style="background:#DCFCE7; color:var(--locar-green-dark); border-color:#86EFAC;">
                📤 Importar Malha GeoJSON
            </button>
            <input type="file" id="inputImportGeoJson" accept=".geojson,.json" style="display:none;" />
        </div>

        <!-- SIDEBAR DIREITA -->
        <div class="sidebar" id="sidebarRight">
            <div class="sidebar-mobile-header" id="sidebarMobileHeader">
                <span>📊 Painel de Filtros e Legenda</span>
                <span id="sidebarMobileIcon">▼</span>
            </div>
            <div id="panelAuditoria">
                <div class="sidebar-section">
                    <h4>Filtros de Coleta</h4>
                    <div class="sidebar-field">
                        <label for="filterColeta">Por coleta</label>
                        <select id="filterColeta">
                            <option value="all" selected>Coleta (Domiciliar + Manual)</option>
                            <option value="domiciliar">Coleta Domiciliar</option>
                            <option value="possible_manual">Coleta Manual</option>
                            <option value="collected">Coletadas (Executadas)</option>
                            <option value="not_collected">Não coletadas (Crítica)</option>
                            <option value="possible_gps_gap">Possível falha de GPS</option>
                        </select>
                    </div>
                    <div class="sidebar-field">
                        <label for="filterTurno">Turno (Diurno / Noturno)</label>
                        <select id="filterTurno">
                            <option value="all" selected>Todos (Diurno + Noturno)</option>
                            <option value="DIURNO">☀️ Diurno (Setores D)</option>
                            <option value="NOTURNO">🌙 Noturno (Setores N)</option>
                        </select>
                    </div>
                    <div class="sidebar-field" style="position:relative;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                            <label style="margin-bottom:0;">Por setor (Multi-seleção):</label>
                            <span class="sector-count-pill" id="sectorSelectedCount">Todos</span>
                        </div>
                        <div style="font-size:0.70rem; color:#475569; margin-bottom:5px; font-weight:500;" id="lblSubFrequenciaSetores">
                            Agenda por sufixo (SEG..DOM, A, E, 1..4/L, QZ) e prefixo (SCD*/SCP*/SCI*/SCDOM*)
                        </div>
                        <div class="multi-select-container" id="multiSetorContainer">
                            <button type="button" class="multi-select-trigger" id="btnMultiSetorTrigger">
                                <span class="trigger-text" id="multiSetorLabel">Todos os setores</span>
                                <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                                    <path d="M1.646 4.646a.5.5 0 0 1 .708 0L8 10.293l5.646-5.647a.5.5 0 0 1 .708.708l-6 6a.5.5 0 0 1-.708 0l-6-6a.5.5 0 0 1 0-.708z"/>
                                </svg>
                            </button>
                            <div class="multi-select-dropdown" id="multiSetorDropdown" style="display:none;">
                                <div class="multi-select-header">
                                    <div class="multi-select-actions">
                                        <button type="button" class="btn-chip" id="btnSelectAllSectors">Marcar Todos</button>
                                        <button type="button" class="btn-chip" id="btnClearSectors">Desmarcar Todos</button>
                                    </div>
                                    <input type="text" id="inputSearchSector" class="multi-select-search" placeholder="🔍 Filtrar setor..." />
                                </div>
                                <div class="multi-select-list" id="multiSetorList"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="sidebar-section">
                    <h4>Carros</h4>
                    <div class="sidebar-field">
                        <label for="filterVeiculo">Veículo no mapa</label>
                        <select id="filterVeiculo">
                            <option value="all">Todos os veículos</option>
                        </select>
                        <div class="section-hint">Filtra ruas coletadas e rastro GPS pelo veículo escolhido.</div>
                    </div>
                </div>

                <div class="sidebar-section" id="secCamadasInlog">
                    <h4>Camadas Inlog em Tempo Real</h4>
                    <label class="layer-check-row">
                        <span class="layer-check-left">
                            <input type="checkbox" id="chkLayerVeiculosRT" checked />
                            <span>🚛 Veículos ao Vivo (TempoReal)</span>
                        </span>
                        <span class="layer-pill" id="pillVeiculosRT">0</span>
                    </label>
                    <label class="layer-check-row" id="rowAutoRefreshRT" style="padding-left:26px; font-weight:500; font-size:0.73rem;">
                        <span class="layer-check-left">
                            <input type="checkbox" id="chkAutoRefreshRT" />
                            <span>Auto-atualizar frota (30s)</span>
                        </span>
                        <span style="color:var(--locar-green-dark);" id="lblUltimaAttRT">--:--</span>
                    </label>
                    <label class="layer-check-row">
                        <span class="layer-check-left">
                            <input type="checkbox" id="chkLayerRastroGPS" />
                            <span>🛰️ Rastro GPS Real (Histórico)</span>
                        </span>
                        <span class="layer-pill" id="pillRastroGPS">0 pts</span>
                    </label>
                    <label class="layer-check-row">
                        <span class="layer-check-left">
                            <input type="checkbox" id="chkLayerEventos" />
                            <span>⚠️ Alarmes & Falhas no Mapa</span>
                        </span>
                        <span class="layer-pill" id="pillEventos">0</span>
                    </label>
                    <label class="layer-check-row">
                        <span class="layer-check-left">
                            <input type="checkbox" id="chkLayerRotaPlanejada" checked />
                            <span>🧭 Rota Planejada (Veículo/Setor)</span>
                        </span>
                        <span class="layer-pill" id="pillRotaPlanejada">--</span>
                    </label>
                    <label class="layer-check-row">
                        <span class="layer-check-left">
                            <input type="checkbox" id="chkLayerMalhaBase" />
                            <span>🛣️ Malha Viária Base (.gpkg)</span>
                        </span>
                        <span class="layer-pill" id="pillMalhaBase">Zoom 13+</span>
                    </label>
                </div>

                <div class="sidebar-section">
                    <h4>Visualizações do Projeto</h4>
                    <div class="sidebar-field">
                        <label>🏷 Códigos dos setores (Hover)</label>
                        <div class="toggle-group">
                            <button type="button" class="toggle-btn active" id="btnShowLabels">Mostrar</button>
                            <button type="button" class="toggle-btn" id="btnHideLabels">Ocultar</button>
                        </div>
                    </div>
                    <div class="sidebar-field" style="margin-top:10px;">
                        <label>🌟 Ruas Prioritárias (Borda Azul)</label>
                        <div class="toggle-group">
                            <button type="button" class="toggle-btn active" id="btnShowPriority">Destacar (Azul)</button>
                            <button type="button" class="toggle-btn" id="btnHidePriority">Ocultar</button>
                        </div>
                    </div>
                </div>

                <div class="sidebar-section">
                    <h4 id="legendTitle">Legenda do Mapa de Execução</h4>
                    <div id="legendDiario" class="legend-compact">
                        <div class="legend-row">
                            <span><span class="legend-color-dot" style="background:#00A859;"></span> Coletada</span>
                            <strong id="legColetada">--</strong>
                        </div>
                        <div class="legend-row">
                            <span><span class="legend-color-dot" style="background:#E53935;"></span> Não coletada (Omissão)</span>
                            <strong id="legNaoColetada" style="color:var(--danger-red);">--</strong>
                        </div>
                        <div class="legend-row">
                            <span><span class="legend-color-dot" style="background:#0284C7;"></span> Coleta Manual</span>
                            <strong id="legManual" style="color:#0284C7;">--</strong>
                        </div>
                        <div class="legend-row">
                            <span><span class="legend-color-dot" style="background:#FBC02D;"></span> Falha de GPS</span>
                            <strong id="legGps">--</strong>
                        </div>
                        <div class="legend-row" style="border-top:1px dashed var(--gray-border); padding-top:5px;">
                            <span><span class="legend-color-dot" style="background:#1D4ED8;"></span> Rua Prioritária</span>
                            <strong id="legPrioritaria" style="color:#1D4ED8;">--</strong>
                        </div>
                    </div>
                    <div id="legendPeriodo" class="legend-compact" style="display:none;">
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#00E600;"></span> 80-100%</span><strong id="legP80">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#FFFF00;border:1px solid #cbd5e1;"></span> 60-80%</span><strong id="legP60">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#FF9900;"></span> 40-60%</span><strong id="legP40">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#FF0000;"></span> 20-40%</span><strong id="legP20">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#660000;"></span> 0-20%</span><strong id="legP0">--</strong></div>
                        <div style="border-top:1px dashed var(--gray-border); margin:4px 0; padding-top:4px; font-size:0.7rem; font-weight:700; color:#0284C7;">Coleta Manual (Azul por %)</div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#00FFFF;border:1px solid #94a3b8;"></span> Manual: 80-100%</span><strong id="legM80">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#00BFFF;"></span> Manual: 60-80%</span><strong id="legM60">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#0080FF;"></span> Manual: 40-60%</span><strong id="legM40">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#0040FF;"></span> Manual: 20-40%</span><strong id="legM20">--</strong></div>
                        <div class="legend-row"><span><span class="legend-color-dot" style="background:#001F66;"></span> Manual: 0-20%</span><strong id="legM0">--</strong></div>
                    </div>
                </div>
            </div>

            <!-- PAINEL 2: EDITOR DO PROJETO DA CIDADE (MODO EDICAO) -->
            <div id="panelEdicaoGIS" style="display:none;">
                <div class="sidebar-section" style="background:#FEF3C7; border-left:4px solid var(--edit-amber);">
                    <h4 style="color:var(--edit-amber-dark); margin-bottom:4px;">✏ Edição do Projeto da Cidade</h4>
                    <p style="font-size:0.74rem; color:#78350F;">
                        As alterações modificam o projeto local no SQLite (<b>locar.db</b>) e recalculam a auditoria real contra o GPS da Inlog.
                    </p>
                </div>

                <div class="sidebar-section">
                    <h4>Atributos do Trecho no Projeto</h4>
                    <div class="sidebar-field">
                        <label>ID do Segmento:</label>
                        <input type="text" id="editId" readonly style="background:#F1F5F9; color:#64748B;" value="Nenhum selecionado" />
                    </div>
                    <div class="sidebar-field">
                        <label for="editNomeRua">Nome do Logradouro / Rua:</label>
                        <input type="text" id="editNomeRua" placeholder="Ex: Rua Nova Esperança" />
                    </div>
                    <div class="sidebar-field">
                        <label for="editSetorInput">Setor do Projeto:</label>
                        <input type="text" id="editSetorInput" list="datalistSetores" placeholder="Ex: SCDD 14" />
                        <datalist id="datalistSetores"></datalist>
                    </div>
                    <div class="sidebar-field">
                        <label for="editTipo">Tipo de Coleta:</label>
                        <select id="editTipo">
                            <option value="COLETA">COLETA REGULAR (Mandatory)</option>
                            <option value="MANUAL">COLETA MANUAL (Beco/Escadaria)</option>
                            <option value="DESLOCAMENTO">DESLOCAMENTO (Acesso)</option>
                        </select>
                    </div>
                    <div class="sidebar-field" style="display:flex; align-items:center; gap:8px; margin-top:8px;">
                        <input type="checkbox" id="editIsPriority" style="width:auto;" />
                        <label for="editIsPriority" style="margin-bottom:0; cursor:pointer;">🌟 Rua Prioritária (Avenida / Comércio)</label>
                    </div>
                    <div class="sidebar-field" style="margin-top:10px;">
                        <label>Extensão Calculada:</label>
                        <div style="font-size:0.96rem; font-weight:700; color:var(--text-dark);" id="editExtensao">0.0 metros</div>
                    </div>
                    <div style="display:flex; flex-direction:column; gap:8px; margin-top:14px;">
                        <button class="btn-locar" id="btnSalvarTrecho" style="width:100%; justify-content:center;">
                            💾 Salvar no Projeto da Cidade
                        </button>
                        <button class="btn-outline" id="btnExcluirTrecho" style="width:100%; justify-content:center; color:var(--danger-red); border-color:#FECACA;">
                            🗑 Excluir este Trecho do Projeto
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- LOADING OVERLAY -->
        <div id="loadingOverlay">
            <div class="spinner"></div>
            <div class="loading-text" id="loadingText">Sincronizando dados reais da Inlog...</div>
        </div>
    </div>

    <!-- MODAL CENTRAL INLOG (10 ENDPOINTS) -->
    <div id="modalCentralInlog" class="modal-backdrop" style="display:none;">
        <div class="modal-card modal-xl">
            <div class="modal-header">
                <div style="display:flex; align-items:center; gap:10px;">
                    <div class="modal-icon-badge" style="background:#EEF2FF; color:var(--inlog-indigo);">📡</div>
                    <div>
                        <h3 class="modal-title">Central de Dados Inlog · <span id="centralCidadeLabel">--</span> (<span id="centralDataLabel">--</span>)</h3>
                        <p class="modal-subtitle">Dados oficiais em tempo real e históricos sincronizados diretamente da Inlog.Integration.Coleta.Api</p>
                    </div>
                </div>
                <button type="button" class="modal-close-btn" id="btnCloseCentralInlog">&times;</button>
            </div>

            <div class="tabs-bar" id="centralTabsBar">
                <button class="tab-btn active" data-tab="frota">🚛 Frota & Tempo Real</button>
                <button class="tab-btn" data-tab="comparacao">🧭 Planejado x Executado</button>
                <button class="tab-btn" data-tab="setores">🗺️ Setores Inlog</button>
                <button class="tab-btn" data-tab="rd">📋 Relatórios Diários (RD)</button>
                <button class="tab-btn" data-tab="eventos">⚠️ Alarmes & Falhas</button>
                <button class="tab-btn" data-tab="telemetria">🌿 Telemetria & Motoristas</button>
                <button class="tab-btn" data-tab="logs">🔍 Diagnóstico HTTP / Logs</button>
            </div>

            <div class="modal-body" id="centralTabBody">
                <!-- Preenchido dinamicamente -->
            </div>

            <div class="modal-footer">
                <span style="margin-right:auto; font-size:0.74rem; color:var(--text-muted);" id="centralFooterInfo"></span>
                <button type="button" class="btn-outline" id="btnRefreshCentralTab">🔄 Atualizar Aba da API</button>
                <button type="button" class="btn-locar" id="btnFecharCentralInlog">Fechar</button>
            </div>
        </div>
    </div>

    <!-- MODAL VISÃO GERAL MULTI-CIDADES -->
    <div id="modalVisaoGeral" class="modal-backdrop" style="display:none;">
        <div class="modal-card modal-lg">
            <div class="modal-header">
                <div style="display:flex; align-items:center; gap:10px;">
                    <div class="modal-icon-badge">🌎</div>
                    <div>
                        <h3 class="modal-title">Monitoramento Geral da Operação Locar (Todas as Cidades)</h3>
                        <p class="modal-subtitle">Status ao vivo da frota na Inlog, setores ativos e cobertura auditada na data selecionada</p>
                    </div>
                </div>
                <button type="button" class="modal-close-btn" id="btnCloseVisaoGeral">&times;</button>
            </div>
            <div class="modal-body" id="visaoGeralBody">
                Carregando visão geral...
            </div>
            <div class="modal-footer">
                <button type="button" class="btn-outline" id="btnRefreshVisaoGeral">🔄 Atualizar</button>
                <button type="button" class="btn-locar" id="btnFecharVisaoGeral">Fechar</button>
            </div>
        </div>
    </div>

    <!-- MODAL DE COMPARTILHAMENTO -->
    <div id="modalShareLink" class="modal-backdrop" style="display:none;">
        <div class="modal-card">
            <div class="modal-header">
                <div style="display:flex; align-items:center; gap:10px;">
                    <div class="modal-icon-badge">🔗</div>
                    <div>
                        <h3 class="modal-title">Gerar Link Personalizado de Visualização</h3>
                        <p class="modal-subtitle">Link seguro em modo somente leitura</p>
                    </div>
                </div>
                <button type="button" class="modal-close-btn" id="btnCloseModalShare">&times;</button>
            </div>
            <div class="modal-body">
                <div class="security-callout">
                    <div style="font-weight:700; color:#1D4ED8;">🔒 Link Exclusivo Assinado (Cidade Única)</div>
                    <p style="margin:4px 0 0 0; font-size:0.74rem; color:#1E293B;">
                        Link criptografado restrito apenas à cidade e ao período selecionados. Quem acessa o link não pode alterar a exibição, período ou filtros, restando apenas visualização e download CSV.
                    </p>
                </div>
                <div class="modal-form-grid">
                    <div class="sidebar-field">
                        <label>Cidade Exclusiva do Link:</label>
                        <select id="modalShareCidade"></select>
                    </div>
                    <div class="sidebar-field">
                        <label id="lblModalShareData">Data da Operação:</label>
                        <input type="date" id="modalShareData" />
                        <input type="month" id="modalShareMes" style="display:none;" />
                    </div>
                </div>
                <div class="sidebar-field" style="margin-top:10px;">
                    <label>Período de Exibição / Cálculo (% por Rua e Setor):</label>
                    <select id="modalSharePeriodo">
                        <option value="diario">📅 Diário (Dia Único)</option>
                        <option value="quinzena1">📆 1ª Quinzena (Dias 01 a 15)</option>
                        <option value="quinzena2">📆 2ª Quinzena (Dias 16 ao fim do mês)</option>
                        <option value="mensal">🗓️ Mensal (Mês Completo)</option>
                    </select>
                </div>
                <div class="modal-form-grid" style="margin-top:10px;">
                    <div class="sidebar-field">
                        <label>Turno Inicial (D / N):</label>
                        <select id="modalShareTurno">
                            <option value="all">Todos (Diurno + Noturno)</option>
                            <option value="DIURNO">☀️ Diurno (Setores D)</option>
                            <option value="NOTURNO">🌙 Noturno (Setores N)</option>
                        </select>
                    </div>
                    <div class="sidebar-field">
                        <label>Filtro de Coleta Inicial:</label>
                        <select id="modalShareColeta">
                            <option value="all">Coleta (Domiciliar + Manual)</option>
                            <option value="domiciliar">Coleta Domiciliar</option>
                            <option value="possible_manual">Coleta Manual</option>
                            <option value="not_collected">Apenas Ruas Omitidas</option>
                            <option value="collected">Apenas Coletadas</option>
                            <option value="possible_gps_gap">Apenas Falhas de GPS</option>
                        </select>
                    </div>
                </div>
                <div class="sidebar-field" style="margin-top:10px;">
                    <label>Setores incluídos na visão inicial:</label>
                    <div id="modalShareSetoresResumo" class="share-sectors-summary">Todos os setores</div>
                </div>
                <div class="sidebar-field" style="margin-top:12px;">
                    <label>Link Rápido (Snapshot Congelado):</label>
                    <div class="share-input-group">
                        <input type="text" id="inputGeneratedShareUrl" readonly class="share-url-input" placeholder="Gerando snapshot congelado..." />
                        <button type="button" class="btn-outline" id="btnForcarRecriarShare" title="Recriar snapshot com dados atualizados">🔄 Atualizar</button>
                        <button type="button" class="btn-locar" id="btnCopyGeneratedUrl">📋 Copiar</button>
                    </div>
                    <div id="shareSnapshotStatus" style="margin-top:6px; font-size:0.74rem; color:#475569;"></div>
                </div>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn-outline" id="btnTestOpenShareUrl">↗ Abrir em Nova Aba</button>
                <button type="button" class="btn-outline" id="btnCancelShareModal">Fechar</button>
            </div>
        </div>
    </div>

    <div id="locarToast" class="locar-toast">✔ Ação concluída!</div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        let map;
        let canvasRenderer;
        let geojsonLayer = null;
        let sectorLabelsLayer = null;
        let priorityHaloLayer = null;
        let vehiclesRtLayer = null;
        let gpsTrailLayer = null;
        let eventsLayer = null;
        let plannedRouteLayer = null;
        let baseMeshLayer = null;
        let baseMeshMoveTimer = null;

        let drawStartMarker = null;
        let drawEndMarker = null;
        let drawActivePolyline = null;
        let drawGuidePolyline = null;
        let drawingCoordinates = [];

        let currentAudit = null;
        let currentFeatures = [];
        let setoresList = [];
        let setorTurnoMap = {};
        let selectedSectors = new Set();
        let lastCenteredCity = null;
        let isReadOnlyMode = false;
        let isSharedLinkMode = false;
        let sharedToken = null;
        let sharedSnapshotId = null;
        let sharedCongeladoEm = null;
        let sharedPeriodo = 'diario';
        let showSectorLabels = true;
        let showPriorityHighlight = true;
        let isEditMode = false;
        let selectedFeature = null;
        let activeGisTool = 'select';
        let autoRefreshTimer = null;
        let activeCentralTab = 'frota';

        const CIDADES_MAPA = {
            'all': 'Todas as Cidades (Visão Geral)',
            'jaboatao': 'Jaboatão dos Guararapes',
            'cabo': 'Cabo de Santo Agostinho',
            'olinda': 'Olinda',
            'paulista': 'Paulista',
            'caruaru': 'Caruaru',
            'cuiaba': 'Cuiabá',
            'garanhuns': 'Garanhuns',
            'pesqueira': 'Pesqueira',
            'vitoria': 'Vitória de Santo Antão',
            'canela': 'Canela',
            'jequie': 'Jequié',
            'novo hamburgo': 'Novo Hamburgo'
        };

        const CIDADES_COORDENADAS = {
            'all': { centro: [-14.2350, -51.9253], zoom: 4 },
            'cabo': { centro: [-8.2833, -35.0333], zoom: 12 },
            'jaboatao': { centro: [-8.1930, -34.9364], zoom: 12 },
            'olinda': { centro: [-7.9986, -34.8459], zoom: 13 },
            'paulista': { centro: [-7.9408, -34.8728], zoom: 12.5 },
            'caruaru': { centro: [-8.2838, -35.9761], zoom: 12 },
            'cuiaba': { centro: [-15.6014, -56.0979], zoom: 12 },
            'garanhuns': { centro: [-8.8907, -36.4966], zoom: 13 },
            'pesqueira': { centro: [-8.3586, -36.6961], zoom: 13 },
            'vitoria': { centro: [-8.1189, -35.2928], zoom: 13 },
            'canela': { centro: [-29.3644, -50.8119], zoom: 13 },
            'jequie': { centro: [-13.8575, -40.0839], zoom: 13 },
            'novo hamburgo': { centro: [-29.6789, -51.1306], zoom: 12.5 }
        };

        function obterTurnoSetor(codigoSetor) {
            if (codigoSetor && setorTurnoMap[codigoSetor]) {
                return setorTurnoMap[codigoSetor];
            }
            const s = String(codigoSetor || '').toUpperCase().trim();
            if (!s) return 'DIURNO';
            if (/\b(SCDOMN|SCDN|SCPN|SCIN|SCDM|SCPM|SCIM)\b/.test(s)) return 'NOTURNO';
            if (/\b(SCDOMD|SCDD|SCPD|SCID|SCAD|SCSBD|SCSCD|SCPV|SCIV|SCDV)\b/.test(s)) return 'DIURNO';
            const mDom = s.match(/\bSCDOM\s*0*(\d+)\b/);
            if (mDom && (mDom[1] === '28' || mDom[1] === '29' || mDom[1] === '38')) return 'NOTURNO';
            if (s.includes('NOTURNO') || s.includes('NOITE')) return 'NOTURNO';
            const m = s.match(/\bSC[A-Z]([A-Z])(?:\b|\s|\d)/);
            if (m && s.indexOf('SCDOM') === -1) {
                const l4 = m[1];
                if (l4 === 'N' || l4 === 'M') return 'NOTURNO';
                if (l4 === 'D' || l4 === 'V') return 'DIURNO';
            }
            return 'DIURNO';
        }

        function escapeHtml(str) {
            if (str === null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function formatDataHojeISO() {
            const d = new Date();
            const yyyy = d.getFullYear();
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const dd = String(d.getDate()).padStart(2, '0');
            return `${yyyy}-${mm}-${dd}`;
        }

        function mostrarToast(msg) {
            const toast = document.getElementById('locarToast');
            if (!toast) return;
            toast.innerText = '✔ ' + msg;
            toast.style.display = 'flex';
            clearTimeout(toast._tmr);
            toast._tmr = setTimeout(() => { toast.style.display = 'none'; }, 3500);
        }

        function ativarModoLinkCompartilhado(lockCfg) {
            isReadOnlyMode = true;
            isSharedLinkMode = true;
            sharedToken = (lockCfg && lockCfg.token) ? lockCfg.token : null;
            sharedSnapshotId = (lockCfg && lockCfg.snapshot_id) ? lockCfg.snapshot_id : null;
            sharedCongeladoEm = (lockCfg && lockCfg.congelado_em) ? lockCfg.congelado_em : null;
            sharedPeriodo = (lockCfg && lockCfg.periodo) ? String(lockCfg.periodo).toLowerCase() : 'diario';
            document.body.classList.add('modo-link-compartilhado');

            const btnEdit = document.getElementById('btnToggleEditMode');
            if (btnEdit) btnEdit.style.display = 'none';
            const gisToolbar = document.getElementById('gisToolbar');
            if (gisToolbar) gisToolbar.style.display = 'none';
            const panelEdicao = document.getElementById('panelEdicaoGIS');
            if (panelEdicao) panelEdicao.style.display = 'none';

            // Desativa camadas em tempo real extras no link gerado
            ['chkLayerVeiculosRT', 'chkLayerRastroGPS', 'chkLayerEventos', 'chkLayerRotaPlanejada', 'chkLayerMalhaBase'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.checked = false;
            });

            // Trava selectCidade apenas na cidade do link
            const cidadeLock = String((lockCfg && lockCfg.cidade) || '').toLowerCase().trim();
            if (cidadeLock) {
                const selectCidade = document.getElementById('selectCidade');
                if (selectCidade) {
                    Array.from(selectCidade.options).forEach(opt => {
                        if (opt.value.toLowerCase() !== cidadeLock) opt.remove();
                    });
                    selectCidade.value = cidadeLock;
                }
            }
            if (lockCfg && lockCfg.data) {
                document.getElementById('inputData').value = lockCfg.data;
            }
            if (lockCfg && lockCfg.turno) {
                const st = document.getElementById('selectTurno');
                const ft = document.getElementById('filterTurno');
                if (st) st.value = lockCfg.turno;
                if (ft) ft.value = lockCfg.turno;
            }
            if (lockCfg && lockCfg.coleta) {
                const fc = document.getElementById('filterColeta');
                if (fc) fc.value = lockCfg.coleta;
            }
            if (lockCfg && Array.isArray(lockCfg.setores) && lockCfg.setores.length > 0) {
                selectedSectors = new Set(lockCfg.setores);
            }

            // Bloqueia controles de alteração de exibição para quem acessa o link
            ['selectCidade', 'inputData', 'selectTurno', 'selectOperacao', 'filterTurno', 'filterColeta', 'filterVeiculo'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.disabled = true;
            });
            const msList = document.getElementById('multiSetorList');
            if (msList) {
                msList.style.pointerEvents = 'none';
                msList.style.opacity = '0.7';
            }
        }

        function checkUrlParams() {
            const urlParams = new URLSearchParams(window.location.search);
            document.getElementById('inputData').value = formatDataHojeISO();
            document.getElementById('selectOperacao').value = 'COLETA';

            const lock = window.__SHARED_LOCK__;
            if (lock && lock.active) {
                ativarModoLinkCompartilhado(lock);
                return;
            }

            if (urlParams.get('readonly') === '1' || urlParams.get('modo') === 'view' || urlParams.get('view') === '1') {
                const cidadeParam = (urlParams.get('cidade') || 'cabo').toLowerCase();
                ativarModoLinkCompartilhado({
                    active: true,
                    cidade: cidadeParam,
                    data: urlParams.get('data') || '',
                    turno: urlParams.get('turno') || 'all',
                    coleta: urlParams.get('coleta') || 'all',
                    setores: urlParams.has('setores') ? urlParams.get('setores').split(',').map(s => s.trim()).filter(Boolean) : []
                });
                return;
            }

            if (urlParams.has('cidade')) {
                const c = urlParams.get('cidade').toLowerCase();
                const selectCidade = document.getElementById('selectCidade');
                if (selectCidade) selectCidade.value = c;
            } else {
                const selectCidade = document.getElementById('selectCidade');
                if (selectCidade && Array.from(selectCidade.options).some(o => o.value === 'cabo')) {
                    selectCidade.value = 'cabo';
                }
            }

            if (urlParams.has('data')) {
                document.getElementById('inputData').value = urlParams.get('data');
            }
            if (urlParams.has('turno')) {
                const t = urlParams.get('turno');
                document.getElementById('selectTurno').value = t;
                document.getElementById('filterTurno').value = t;
            }
            if (urlParams.has('coleta')) {
                document.getElementById('filterColeta').value = urlParams.get('coleta');
            }
            if (urlParams.has('setores')) {
                const parts = urlParams.get('setores').split(',').map(s => s.trim()).filter(Boolean);
                selectedSectors = new Set(parts);
            }
        }

        function initMap() {
            canvasRenderer = L.canvas({ padding: 0.5 });
            map = L.map('map', { preferCanvas: true, zoomControl: true, zoomSnap: 0.25 }).setView([-8.2833, -35.0333], 12);

            map.createPane('baseMeshPane');
            map.getPane('baseMeshPane').style.zIndex = 340;
            map.getPane('baseMeshPane').style.pointerEvents = 'none';

            map.createPane('priorityHaloPane');
            map.getPane('priorityHaloPane').style.zIndex = 390;
            map.getPane('priorityHaloPane').style.pointerEvents = 'none';

            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; OpenStreetMap · Locar Saneamento & Inlog API',
                maxZoom: 19,
                crossOrigin: 'anonymous'
            }).addTo(map);

            baseMeshLayer = L.layerGroup().addTo(map);
            priorityHaloLayer = L.featureGroup().addTo(map);
            plannedRouteLayer = L.layerGroup().addTo(map);
            gpsTrailLayer = L.layerGroup().addTo(map);
            sectorLabelsLayer = L.layerGroup().addTo(map);
            vehiclesRtLayer = L.layerGroup().addTo(map);
            eventsLayer = L.layerGroup().addTo(map);

            map.on('moveend', () => {
                if (!isSharedLinkMode && document.getElementById('chkLayerMalhaBase')?.checked) {
                    clearTimeout(baseMeshMoveTimer);
                    baseMeshMoveTimer = setTimeout(carregarMalhaBaseMapa, 250);
                }
            });
            map.on('click', onMapClickForDrawing);
            map.on('mousemove', onMapMouseMoveForDrawing);
        }

        async function carregarAuditoria(forcar = false) {
            const cidade = document.getElementById('selectCidade').value;
            const operacaoSelecionada = document.getElementById('selectOperacao').value;
            const operacaoApi = 'COLETA';
            const data = document.getElementById('inputData').value;

            document.getElementById('loadingOverlay').style.display = 'flex';
            document.getElementById('loadingText').innerText = sharedSnapshotId
                ? `Carregando mapa congelado de ${CIDADES_MAPA[cidade] || cidade}...`
                : `Sincronizando ${CIDADES_MAPA[cidade] || cidade} com a API Inlog...`;

            try {
                let url;
                if (sharedSnapshotId) {
                    url = `/api/snapshot/${encodeURIComponent(sharedSnapshotId)}`;
                } else {
                    url = `/api/auditoria?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&operacao=${encodeURIComponent(operacaoApi)}&force=${forcar}&_t=${Date.now()}`;
                    if (sharedPeriodo && sharedPeriodo !== 'diario') url += `&periodo=${encodeURIComponent(sharedPeriodo)}`;
                    if (sharedToken) url += `&token=${encodeURIComponent(sharedToken)}`;
                }
                const resp = await fetch(url);
                const dataRes = await resp.json();

                if (dataRes.erro) {
                    mostrarToast('Aviso: ' + dataRes.erro);
                    return;
                }

                currentAudit = dataRes;
                currentFeatures = dataRes.geojson.features || [];
                setoresList = dataRes.setores || [];
                setorTurnoMap = {};
                setoresList.forEach(s => {
                    if (s && s.codigo && s.turno) setorTurnoMap[s.codigo] = s.turno;
                });

                if (isSharedLinkMode) {
                    const tagEl = document.getElementById('headerTagline');
                    if (tagEl) {
                        const dtFmt = dataRes.resumo?.data_formatada || data;
                        const perRot = dataRes.resumo?.periodo_rotulo ? `${dataRes.resumo.periodo_rotulo} · ` : '';
                        const congStr = (sharedCongeladoEm || dataRes.resumo?.snapshot?.congelado_em || '').replace('T', ' ').slice(0, 16);
                        const congRot = congStr ? ` · Congelado ${congStr}` : '';
                        tagEl.innerText = `· ${CIDADES_MAPA[cidade] || cidade} (${perRot}${dtFmt})${congRot}`;
                    }
                }

                if (lastCenteredCity !== cidade) {
                    if (!isSharedLinkMode) selectedSectors.clear();
                    const c = dataRes.resumo.centro || [-8.1930, -34.9364];
                    const z = dataRes.resumo.zoom || 12;
                    map.setView(c, z, { animate: true });
                    lastCenteredCity = cidade;
                }

                const banner = document.getElementById('noMeshBanner');
                if (dataRes.resumo.sem_malha_viaria) {
                    banner.style.display = 'block';
                    if (!isSharedLinkMode) {
                        document.getElementById('chkLayerVeiculosRT').checked = true;
                        document.getElementById('chkLayerRastroGPS').checked = true;
                    }
                } else {
                    banner.style.display = 'none';
                }

                if (!isSharedLinkMode) {
                    if (operacaoSelecionada === 'DOMICILIAR') document.getElementById('filterColeta').value = 'domiciliar';
                    else if (operacaoSelecionada === 'MANUAL') document.getElementById('filterColeta').value = 'possible_manual';
                }

                popularSelectsFiltros(dataRes);
                renderizarResultado();
                await atualizarCamadasInlog(forcar);
            } catch (err) {
                console.error(err);
                mostrarToast('Erro de comunicação com o servidor local.');
            } finally {
                document.getElementById('loadingOverlay').style.display = 'none';
            }
        }

        async function atualizarCamadasInlog(force = false) {
            if (isSharedLinkMode) {
                if (vehiclesRtLayer) vehiclesRtLayer.clearLayers();
                if (gpsTrailLayer) gpsTrailLayer.clearLayers();
                if (eventsLayer) eventsLayer.clearLayers();
                if (plannedRouteLayer) plannedRouteLayer.clearLayers();
                if (baseMeshLayer) baseMeshLayer.clearLayers();
                return;
            }
            const promises = [];
            if (document.getElementById('chkLayerVeiculosRT').checked) {
                promises.push(carregarVeiculosTempoRealMapa(force));
            } else {
                vehiclesRtLayer.clearLayers();
            }
            if (document.getElementById('chkLayerRastroGPS').checked) {
                promises.push(carregarRastroGpsMapa());
            } else {
                gpsTrailLayer.clearLayers();
            }
            if (document.getElementById('chkLayerEventos').checked) {
                promises.push(carregarEventosMapa(force));
            } else {
                eventsLayer.clearLayers();
            }
            if (document.getElementById('chkLayerRotaPlanejada')?.checked) {
                promises.push(carregarRotaPlanejadaMapa());
            } else if (plannedRouteLayer) {
                plannedRouteLayer.clearLayers();
            }
            if (document.getElementById('chkLayerMalhaBase')?.checked) {
                promises.push(carregarMalhaBaseMapa());
            } else if (baseMeshLayer) {
                baseMeshLayer.clearLayers();
            }
            await Promise.all(promises);
        }

        async function carregarRotaPlanejadaMapa() {
            if (!plannedRouteLayer) return;
            plannedRouteLayer.clearLayers();
            const pill = document.getElementById('pillRotaPlanejada');
            const cidade = document.getElementById('selectCidade').value;
            const data = document.getElementById('inputData').value;
            const placa = document.getElementById('filterVeiculo').value;
            const setor = selectedSectors.size === 1 ? Array.from(selectedSectors)[0] : '';

            if (placa === 'all' && !setor) {
                if (pill) pill.innerText = 'Selecione carro/setor';
                return;
            }
            try {
                const url = `/api/rota-planejada?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&placa=${encodeURIComponent(placa === 'all' ? '' : placa)}&setor=${encodeURIComponent(setor)}`;
                const resp = await fetch(url);
                const res = await resp.json();
                const feats = res.geojson?.features || [];
                if (pill) pill.innerText = `${res.km_coleta || 0} km`;

                feats.forEach(f => {
                    const p = f.properties || {};
                    const coords = (f.geometry?.coordinates || []).map(pt => [pt[1], pt[0]]);
                    if (coords.length < 2) return;
                    const ehColeta = p.is_collection !== false;
                    L.polyline(coords, {
                        renderer: canvasRenderer,
                        color: ehColeta ? '#4F46E5' : '#9333EA',
                        weight: ehColeta ? 4.5 : 2.5,
                        opacity: ehColeta ? 0.85 : 0.55,
                        dashArray: ehColeta ? '8, 6' : '3, 5'
                    }).bindTooltip(
                        `<b>Rota Planejada: ${escapeHtml(p.rota || '')}</b><br/>` +
                        `Rua: ${escapeHtml(p.nome || '')}<br/>` +
                        `Seq: #${p.seq || '-'} · ${ehColeta ? 'Coleta' : 'Deslocamento'} (${Number(p.len_m||0).toFixed(0)} m)`
                    ).addTo(plannedRouteLayer);
                });
            } catch (e) {
                console.error('Erro rota planejada:', e);
            }
        }

        async function carregarMalhaBaseMapa() {
            if (!baseMeshLayer || !map) return;
            const pill = document.getElementById('pillMalhaBase');
            if (map.getZoom() < 13) {
                baseMeshLayer.clearLayers();
                if (pill) pill.innerText = 'Zoom 13+';
                return;
            }
            const cidade = document.getElementById('selectCidade').value;
            const b = map.getBounds();
            const bbox = `${b.getWest().toFixed(6)},${b.getSouth().toFixed(6)},${b.getEast().toFixed(6)},${b.getNorth().toFixed(6)}`;
            try {
                const resp = await fetch(`/api/malha-viaria?cidade=${encodeURIComponent(cidade)}&bbox=${encodeURIComponent(bbox)}`);
                const res = await resp.json();
                baseMeshLayer.clearLayers();
                const feats = res.features || [];
                if (pill) pill.innerText = `${feats.length} vias`;
                feats.forEach(f => {
                    const coords = (f.geometry?.coordinates || []).map(pt => [pt[1], pt[0]]);
                    if (coords.length < 2) return;
                    L.polyline(coords, {
                        pane: 'baseMeshPane',
                        renderer: canvasRenderer,
                        color: '#64748B',
                        weight: 1.5,
                        opacity: 0.45
                    }).addTo(baseMeshLayer);
                });
            } catch (e) {
                console.error('Erro malha base:', e);
            }
        }

        async function carregarVeiculosTempoRealMapa(force = false) {
            const cidade = document.getElementById('selectCidade').value;
            const operacao = document.getElementById('selectOperacao').value;
            try {
                const resp = await fetch(`/api/inlog/veiculos-tempo-real?cidade=${encodeURIComponent(cidade)}&operacao=${encodeURIComponent(operacao)}&force=${force}`);
                const data = await resp.json();
                vehiclesRtLayer.clearLayers();
                const veiculos = data.veiculos || [];
                document.getElementById('pillVeiculosRT').innerText = veiculos.length;

                const agora = new Date();
                document.getElementById('lblUltimaAttRT').innerText =
                    `${String(agora.getHours()).padStart(2,'0')}:${String(agora.getMinutes()).padStart(2,'0')}`;

                const filtroVeic = document.getElementById('filterVeiculo').value;
                veiculos.forEach(v => {
                    if (filtroVeic !== 'all' && v.placaVeiculo !== filtroVeic) return;
                    const lat = parseFloat(v.latitude || 0);
                    const lon = parseFloat(v.longitude || 0);
                    if (!lat && !lon) return;

                    const ign = v.statusIgnicaoVeiculo === 'L';
                    const vel = Number(v.velocidade || 0);
                    const cls = !ign ? 'off' : (vel > 1 ? 'moving' : 'on');
                    const grupos = (v.gruposControle || []).map(g => g.descricao).join(', ');
                    const motorista = v.motoristaNome ? `${v.matriculaMotorista || ''} - ${v.motoristaNome}` : (v.matriculaMotorista || 'Não vinculado');
                    const modelo = v.cadastro ? `${v.cadastro.fabricante || ''} ${v.cadastro.modelo || ''} (${v.cadastro.ano || ''})` : (v.tipoVeiculo || '');

                    const icon = L.divIcon({
                        className: 'truck-div-icon',
                        html: `<div class="truck-marker-box">
                                   <div class="truck-dot ${cls}">🚛</div>
                                   <div class="truck-plate-tag">${escapeHtml(v.placaVeiculo)}</div>
                               </div>`,
                        iconSize: [60, 36],
                        iconAnchor: [30, 18]
                    });

                    const popupHtml = `
                        <div style="font-size:12px; min-width:220px; line-height:1.45;">
                            <div style="font-weight:800; font-size:13px; color:#005C2E; border-bottom:1px solid #E2E8F0; padding-bottom:4px; margin-bottom:5px;">
                                🚛 ${escapeHtml(v.placaVeiculo)} · ${escapeHtml(v.tipoVeiculo)}
                            </div>
                            <div><b>Modelo:</b> ${escapeHtml(modelo)}</div>
                            <div><b>Motorista:</b> ${escapeHtml(motorista)}</div>
                            <div><b>Velocidade:</b> ${vel.toFixed(1)} km/h | <b>Ignição:</b> ${ign ? '🟢 Ligada' : '⚪ Desligada'}</div>
                            <div><b>AVL:</b> ${v.statusConexaoAVL === 'T' ? '🟢 Conectado' : '🔴 Desconectado'} | <b>Sensores:</b> ${escapeHtml(v.sensor1||'-')}/${escapeHtml(v.sensor2||'-')}/${escapeHtml(v.sensor3||'-')}</div>
                            <div><b>Grupo:</b> ${escapeHtml(grupos)}</div>
                            <div><b>Última Atualização:</b> ${escapeHtml((v.dataHoraAtualizacao||'').replace('T',' '))}</div>
                        </div>
                    `;
                    L.marker([lat, lon], { icon, zIndexOffset: 900 }).bindPopup(popupHtml).addTo(vehiclesRtLayer);
                });
            } catch (e) {
                console.error('Erro veiculos RT:', e);
            }
        }

        async function carregarRastroGpsMapa() {
            const cidade = document.getElementById('selectCidade').value;
            const operacao = document.getElementById('selectOperacao').value;
            const data = document.getElementById('inputData').value;
            const placa = document.getElementById('filterVeiculo').value;

            try {
                const resp = await fetch(`/api/inlog/rastro-gps?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&operacao=${encodeURIComponent(operacao)}&placa=${encodeURIComponent(placa)}`);
                const res = await resp.json();
                gpsTrailLayer.clearLayers();
                document.getElementById('pillRastroGPS').innerText = `${res.total_pontos || 0} pts`;

                const porPlaca = res.por_placa || {};
                const cores = ['#2563EB', '#7C3AED', '#0891B2', '#DB2777', '#059669', '#D97706', '#4F46E5', '#BE123C'];
                let idx = 0;

                Object.entries(porPlaca).forEach(([pl, pontos]) => {
                    if (!pontos || pontos.length === 0) return;
                    const cor = cores[idx % cores.length];
                    idx++;

                    const latlngs = pontos.map(p => [p.lat, p.lon]);
                    L.polyline(latlngs, {
                        renderer: canvasRenderer,
                        color: cor,
                        weight: 2.5,
                        opacity: 0.78,
                        dashArray: '4, 4'
                    }).bindTooltip(`Rastro GPS: <b>${escapeHtml(pl)}</b> (${pontos.length} pts)`).addTo(gpsTrailLayer);

                    // Amostra pontos (ou todos se <= 1500) para visualização detalhada sem travar DOM
                    const step = pontos.length > 1200 ? Math.ceil(pontos.length / 600) : 1;
                    for (let i = 0; i < pontos.length; i += step) {
                        const pt = pontos[i];
                        L.circleMarker([pt.lat, pt.lon], {
                            renderer: canvasRenderer,
                            radius: 3,
                            color: '#FFFFFF',
                            weight: 1,
                            fillColor: cor,
                            fillOpacity: 0.9
                        }).bindTooltip(
                            `<b>${escapeHtml(pl)}</b><br/>Hora: ${escapeHtml((pt.dataHora||'').replace('T',' '))}<br/>Vel: ${pt.velocidade} km/h`
                        ).addTo(gpsTrailLayer);
                    }
                });
            } catch (e) {
                console.error('Erro rastro GPS:', e);
            }
        }

        async function carregarEventosMapa(force = false) {
            const cidade = document.getElementById('selectCidade').value;
            const data = document.getElementById('inputData').value;
            try {
                const resp = await fetch(`/api/inlog/alarmes-falhas?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&force=${force}`);
                const res = await resp.json();
                eventsLayer.clearLayers();

                const alarmes = res.alarmes || [];
                const falhas = res.falhas || [];
                document.getElementById('pillEventos').innerText = alarmes.length + falhas.length;

                alarmes.forEach(a => {
                    if (!a.latitude && !a.longitude) return;
                    L.circleMarker([a.latitude, a.longitude], {
                        renderer: canvasRenderer,
                        radius: 6,
                        color: '#991B1B',
                        weight: 2,
                        fillColor: '#EF4444',
                        fillOpacity: 0.9
                    }).bindPopup(
                        `<div style="font-size:12px;"><b>⚠️ Alarme: ${escapeHtml(a.descricao)}</b><br/>Placa: ${escapeHtml(a.placa)}<br/>Motorista: ${escapeHtml(a.motorista)}<br/>Início: ${escapeHtml((a.dataHoraInicio||'').replace('T',' '))}<br/>Duração: ${escapeHtml(a.duracao)} | Status: ${escapeHtml(a.status)}</div>`
                    ).addTo(eventsLayer);
                });

                falhas.forEach(f => {
                    if (!f.latitude && !f.longitude) return;
                    L.circleMarker([f.latitude, f.longitude], {
                        renderer: canvasRenderer,
                        radius: 6,
                        color: '#92400E',
                        weight: 2,
                        fillColor: '#F59E0B',
                        fillOpacity: 0.9
                    }).bindPopup(
                        `<div style="font-size:12px;"><b>🔧 Falha: ${escapeHtml(f.descricao)}</b><br/>Placa: ${escapeHtml(f.placa)}<br/>Módulo: ${escapeHtml(f.descricaoFalhaModulo)}<br/>Início: ${escapeHtml((f.dataHoraInicio||'').replace('T',' '))}</div>`
                    ).addTo(eventsLayer);
                });
            } catch (e) {
                console.error('Erro eventos mapa:', e);
            }
        }

        function popularSelectsFiltros(dataRes) {
            const selectVeiculo = document.getElementById('filterVeiculo');
            const datalistSetores = document.getElementById('datalistSetores');
            const multiSetorList = document.getElementById('multiSetorList');

            if (dataRes.resumo && dataRes.resumo.frequencia_dia) {
                const f = dataRes.resumo.frequencia_dia;
                const pillBox = document.getElementById('pillFrequenciaBox');
                const lblRegra = document.getElementById('lblFrequenciaRegra');
                const lblSub = document.getElementById('lblSubFrequenciaSetores');
                if (lblRegra) {
                    const siglas = (f.siglas_esperadas && f.siglas_esperadas.length > 0) ? ` (${f.siglas_esperadas.join('/')})` : '';
                    lblRegra.innerText = `${f.frequencia}${siglas}`;
                }
                if (pillBox) {
                    pillBox.title = `${f.rotulo || f.frequencia} - ${f.descricao || ''}`;
                    if (f.frequencia === 'PAR') {
                        pillBox.style.background = '#ECFDF5'; pillBox.style.color = '#065F46'; pillBox.style.borderColor = '#A7F3D0';
                    } else if (f.frequencia === 'IMPAR') {
                        pillBox.style.background = '#EFF6FF'; pillBox.style.color = '#1E40AF'; pillBox.style.borderColor = '#BFDBFE';
                    } else {
                        pillBox.style.background = '#FEF3C7'; pillBox.style.color = '#92400E'; pillBox.style.borderColor = '#FDE68A';
                    }
                }
                if (lblSub) {
                    lblSub.innerText = f.descricao || `Exibindo apenas setores da frequência ${f.frequencia}.`;
                }
            }

            if (datalistSetores) {
                datalistSetores.innerHTML = '';
                setoresList.forEach(s => {
                    const opt = document.createElement('option');
                    opt.value = s.codigo;
                    datalistSetores.appendChild(opt);
                });
            }

            renderizarListaSetoresFiltro();

            const prevVeic = selectVeiculo.value;
            selectVeiculo.innerHTML = '<option value="all">Todos os veículos</option>';
            const placas = dataRes.resumo.placas_ativas || [];
            const gpsCounts = dataRes.resumo.placas_com_gps || {};
            placas.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p;
                const qtd = gpsCounts[p];
                opt.textContent = qtd !== undefined ? `${p} (${qtd} pts GPS)` : p;
                selectVeiculo.appendChild(opt);
            });
            if (Array.from(selectVeiculo.options).some(o => o.value === prevVeic)) {
                selectVeiculo.value = prevVeic;
            }
        }

        function getSetoresDoTurnoAtual() {
            const turno = document.getElementById('filterTurno')?.value || 'all';
            if (turno === 'all') return setoresList;
            return setoresList.filter(s => obterTurnoSetor(s.codigo) === turno);
        }

        function renderizarListaSetoresFiltro() {
            const multiSetorList = document.getElementById('multiSetorList');
            if (!multiSetorList) return;
            const visiveis = getSetoresDoTurnoAtual();
            multiSetorList.innerHTML = '';
            visiveis.forEach(s => {
                const isChecked = (selectedSectors.size === 0 || selectedSectors.has(s.codigo));
                const item = document.createElement('label');
                item.className = 'multi-select-item' + (isChecked && selectedSectors.size > 0 && selectedSectors.size < visiveis.length ? ' selected' : '');
                item.dataset.code = String(s.codigo || '').toLowerCase();
                item.innerHTML = `
                    <input type="checkbox" value="${escapeHtml(s.codigo)}" ${isChecked ? 'checked' : ''} />
                    <div class="sector-item-info">
                        <span class="sector-code">${escapeHtml(s.codigo)}</span>
                        <span class="sector-meta">${s.agenda_rotulo ? escapeHtml(s.agenda_rotulo) + ' · ' : ''}${s.total_vias || 0} vias · ${s.extensao_km || 0} km</span>
                    </div>
                `;
                item.querySelector('input').addEventListener('change', () => onSectorCheckboxChange());
                multiSetorList.appendChild(item);
            });
            atualizarTriggerSetores();
        }

        function toggleMultiSetorDropdown(forceClose = false) {
            const dd = document.getElementById('multiSetorDropdown');
            const btn = document.getElementById('btnMultiSetorTrigger');
            if (!dd) return;
            if (forceClose || dd.style.display === 'block') {
                dd.style.display = 'none';
                if (btn) btn.classList.remove('active');
            } else {
                dd.style.display = 'block';
                if (btn) btn.classList.add('active');
                const search = document.getElementById('inputSearchSector');
                if (search) { search.value = ''; filtrarItensSetor(''); search.focus(); }
            }
        }

        function filtrarItensSetor(termo) {
            const q = termo.toLowerCase().trim();
            document.querySelectorAll('#multiSetorList .multi-select-item').forEach(it => {
                it.style.display = (!q || (it.dataset.code && it.dataset.code.includes(q))) ? 'flex' : 'none';
            });
        }

        function onSectorCheckboxChange() {
            const checkboxes = document.querySelectorAll('#multiSetorList input[type="checkbox"]');
            const marcados = [];
            checkboxes.forEach(c => {
                if (c.checked) marcados.push(c.value);
                const parent = c.closest('.multi-select-item');
                if (parent) parent.classList.toggle('selected', c.checked && marcados.length < checkboxes.length);
            });
            if (marcados.length === 0 || marcados.length === checkboxes.length) selectedSectors.clear();
            else selectedSectors = new Set(marcados);
            atualizarTriggerSetores();
            aplicarFiltros();
        }

        function atualizarTriggerSetores() {
            const labelEl = document.getElementById('multiSetorLabel');
            const pillEl = document.getElementById('sectorSelectedCount');
            const total = getSetoresDoTurnoAtual().length;
            if (!labelEl || !pillEl) return;

            if (selectedSectors.size === 0 || selectedSectors.size >= total) {
                labelEl.innerText = "Todos os setores";
                pillEl.innerText = `${total} setores`;
                pillEl.classList.remove('active');
            } else if (selectedSectors.size === 1) {
                labelEl.innerText = `Apenas ${Array.from(selectedSectors)[0]}`;
                pillEl.innerText = "1 setor";
                pillEl.classList.add('active');
            } else {
                labelEl.innerText = `${selectedSectors.size} setores selecionados`;
                pillEl.innerText = `${selectedSectors.size} de ${total}`;
                pillEl.classList.add('active');
            }
        }

        function selectAllSectors() {
            document.querySelectorAll('#multiSetorList input[type="checkbox"]').forEach(c => {
                c.checked = true;
                const parent = c.closest('.multi-select-item');
                if (parent) parent.classList.remove('selected');
            });
            selectedSectors.clear();
            atualizarTriggerSetores();
            aplicarFiltros();
        }

        function clearSectors() {
            const checkboxes = document.querySelectorAll('#multiSetorList input[type="checkbox"]');
            checkboxes.forEach(c => {
                c.checked = true;
                const parent = c.closest('.multi-select-item');
                if (parent) parent.classList.remove('selected');
            });
            selectedSectors.clear();
            atualizarTriggerSetores();
            aplicarFiltros();
        }

        function sincronizarCheckboxesSetor() {
            const total = getSetoresDoTurnoAtual().length;
            document.querySelectorAll('#multiSetorList input[type="checkbox"]').forEach(c => {
                const shouldCheck = (selectedSectors.size === 0 || selectedSectors.has(c.value));
                c.checked = shouldCheck;
                const parent = c.closest('.multi-select-item');
                if (parent) parent.classList.toggle('selected', shouldCheck && selectedSectors.size > 0 && selectedSectors.size < total);
            });
            atualizarTriggerSetores();
        }

        function renderizarResultado() {
            const r = currentAudit.resumo;
            const badge = document.getElementById('inlogStatusBadge');
            const isOnline = String(r.status_inlog).toUpperCase().includes('ONLINE');
            document.getElementById('inlogStatusText').innerText = isOnline ? 'INLOG ONLINE' : 'CACHE LOCAL';
            badge.title = r.status_inlog;
            badge.classList.toggle('offline', !isOnline);

            renderizarRotulosSetores();
            aplicarFiltros();
        }

        function renderizarRotulosSetores() {
            sectorLabelsLayer.clearLayers();
            if (!showSectorLabels || !setoresList) return;

            getSetoresDoTurnoAtual().forEach(s => {
                if (s.centro && s.centro[0] !== 0) {
                    const isDestaque = (selectedSectors.size > 0 && selectedSectors.has(s.codigo));
                    const icon = L.divIcon({
                        className: 'sector-custom-marker',
                        html: `<div class="sector-badge-container ${isDestaque ? 'destaque' : ''}">
                                   <div class="sector-dot-marker"></div>
                                   <div class="sector-pill-label">${escapeHtml(s.codigo)}</div>
                               </div>`,
                        iconSize: [20, 20],
                        iconAnchor: [10, 10]
                    });
                    const marker = L.marker(s.centro, { icon });
                    marker.on('click', () => {
                        if (selectedSectors.has(s.codigo) && selectedSectors.size === 1) selectedSectors.clear();
                        else { selectedSectors.clear(); selectedSectors.add(s.codigo); }
                        sincronizarCheckboxesSetor();
                        aplicarFiltros();
                    });
                    sectorLabelsLayer.addLayer(marker);
                }
            });
        }

        function aplicarFiltros() {
            const filtroColeta = document.getElementById('filterColeta').value;
            const filtroTurno = document.getElementById('filterTurno')?.value || 'all';
            const filtroVeiculo = document.getElementById('filterVeiculo').value;

            const r = currentAudit ? currentAudit.resumo : {};
            const ehPeriodo = Boolean(r && r.periodo && r.periodo !== 'diario');

            let featuresFiltradas = [];
            let totalScopeM = 0, coletadoScopeM = 0, omissaoScopeM = 0, manualScopeM = 0;
            let cCol = 0, cOmi = 0, cMan = 0, cGps = 0, cPrio = 0;
            const bP = { p80: 0, p60: 0, p40: 0, p20: 0, p0: 0 };
            const bM = { m80: 0, m60: 0, m40: 0, m20: 0, m0: 0 };

            function corPorPercentual(pctVal, ehMan) {
                if (ehMan) {
                    if (pctVal >= 80) return '#00FFFF';
                    if (pctVal >= 60) return '#00BFFF';
                    if (pctVal >= 40) return '#0080FF';
                    if (pctVal >= 20) return '#0040FF';
                    return '#001F66';
                }
                if (pctVal >= 80) return '#00E600';
                if (pctVal >= 60) return '#FFFF00';
                if (pctVal >= 40) return '#FF9900';
                if (pctVal >= 20) return '#FF0000';
                return '#660000';
            }

            currentFeatures.forEach(f => {
                const p = f.properties;
                const segSectors = (p.sectors && p.sectors.length) ? p.sectors : [p.sector];
                if (!isEditMode) {
                    if (filtroTurno !== 'all') {
                        const atendeTurno = (p.turnos && p.turnos.length)
                            ? p.turnos.includes(filtroTurno)
                            : segSectors.some(s => obterTurnoSetor(s) === filtroTurno);
                        if (!atendeTurno) return;
                    }
                    if (selectedSectors.size > 0 && !segSectors.some(s => selectedSectors.has(s))) return;
                    if (filtroVeiculo !== 'all' && (!p.vehicles || !p.vehicles.includes(filtroVeiculo))) return;
                }

                const len = p.len_m || 0;
                const ehManualEfetiva = (p.status === 'possible_manual') || (Boolean(p.is_manual) && p.status !== 'not_collected');

                if (ehPeriodo) {
                    const dPrev = Math.max(1, Number(p.dias_previstos || 1));
                    const dCol = Number(p.dias_coletados || 0);
                    totalScopeM += len * dPrev;
                    coletadoScopeM += len * dCol;
                    if (ehManualEfetiva) {
                        manualScopeM += len * dCol;
                    } else {
                        omissaoScopeM += len * Math.max(0, dPrev - dCol);
                    }
                } else {
                    totalScopeM += len;
                    if (p.status === 'collected' || p.status === 'possible_manual') {
                        coletadoScopeM += len;
                    } else if (p.status === 'not_collected') {
                        omissaoScopeM += len;
                    }
                    if (ehManualEfetiva) {
                        manualScopeM += len;
                    }
                }

                if (!isEditMode && filtroColeta !== 'all') {
                    if (filtroColeta === 'domiciliar') {
                        if (Boolean(p.is_manual) || p.status === 'possible_manual') return;
                    } else if (filtroColeta === 'possible_manual') {
                        if (!(p.status === 'possible_manual' || Boolean(p.is_manual))) return;
                    } else if (p.status !== filtroColeta) {
                        return;
                    }
                }

                featuresFiltradas.push(f);

                if (ehPeriodo) {
                    const pctSeg = Number(p.pct_periodo ?? 0);
                    if (ehManualEfetiva) {
                        cMan++;
                        if (pctSeg >= 80) bM.m80++;
                        else if (pctSeg >= 60) bM.m60++;
                        else if (pctSeg >= 40) bM.m40++;
                        else if (pctSeg >= 20) bM.m20++;
                        else bM.m0++;
                    } else {
                        if (pctSeg >= 80) bP.p80++;
                        else if (pctSeg >= 60) bP.p60++;
                        else if (pctSeg >= 40) bP.p40++;
                        else if (pctSeg >= 20) bP.p20++;
                        else bP.p0++;
                    }
                    if (pctSeg < 80) cOmi++;
                    else cCol++;
                } else {
                    if (ehManualEfetiva) cMan++;
                    else if (p.status === 'collected') cCol++;
                    else if (p.status === 'not_collected') cOmi++;
                    else if (p.status === 'possible_gps_gap') cGps++;
                }
                if (p.is_priority) cPrio++;
            });

            const pct = totalScopeM > 0 ? (coletadoScopeM / totalScopeM * 100) : 0;
            const pctManual = totalScopeM > 0 ? (manualScopeM / totalScopeM * 100) : 0;
            const manualKm = (manualScopeM / 1000).toFixed(2);

            document.getElementById('kpiCobertura').innerText = r.sem_malha_viaria ? 'Sem malha' : (pct.toFixed(2) + ' %');
            document.getElementById('kpiExtensao').innerText = r.sem_malha_viaria
                ? 'Importe GeoJSON para auditar ruas'
                : `${(coletadoScopeM / 1000).toFixed(2)} / ${(totalScopeM / 1000).toFixed(2)} km coletados`;
            document.getElementById('kpiTotalKm').innerText = r.sem_malha_viaria ? '-- %' : `${pctManual.toFixed(1)} %`;
            document.getElementById('kpiTotalVias').innerText = r.sem_malha_viaria
                ? '0% do total foi coleta manual'
                : `${pctManual.toFixed(1)}% do total foi coleta manual (${manualKm} km)`;
            document.getElementById('kpiOmitidas').innerText = cOmi + ' vias';
            document.getElementById('kpiOmitidasKm').innerText = (omissaoScopeM / 1000).toFixed(2) + ' km pendentes';
            document.getElementById('kpiCarros').innerText = `${(r.placas_ativas || []).length} veículos`;
            document.getElementById('kpiGpsPontos').innerText = `Pontos GPS reais: ${(r.total_pontos_gps || 0).toLocaleString('pt-BR')}`;

            const legDiarioEl = document.getElementById('legendDiario');
            const legPeriodoEl = document.getElementById('legendPeriodo');
            const legTitleEl = document.getElementById('legendTitle');
            if (ehPeriodo) {
                if (legTitleEl) legTitleEl.innerText = `Legenda (${r.periodo_rotulo || 'Período'} por %)`;
                if (legDiarioEl) legDiarioEl.style.display = 'none';
                if (legPeriodoEl) legPeriodoEl.style.display = 'block';
                document.getElementById('legP80').innerText = bP.p80;
                document.getElementById('legP60').innerText = bP.p60;
                document.getElementById('legP40').innerText = bP.p40;
                document.getElementById('legP20').innerText = bP.p20;
                document.getElementById('legP0').innerText = bP.p0;
                document.getElementById('legM80').innerText = bM.m80;
                document.getElementById('legM60').innerText = bM.m60;
                document.getElementById('legM40').innerText = bM.m40;
                document.getElementById('legM20').innerText = bM.m20;
                document.getElementById('legM0').innerText = bM.m0;
            } else {
                if (legTitleEl) legTitleEl.innerText = 'Legenda do Mapa de Execução';
                if (legDiarioEl) legDiarioEl.style.display = 'block';
                if (legPeriodoEl) legPeriodoEl.style.display = 'none';
                document.getElementById('legColetada').innerText = cCol;
                document.getElementById('legNaoColetada').innerText = cOmi;
                document.getElementById('legManual').innerText = cMan;
                document.getElementById('legGps').innerText = cGps;
                document.getElementById('legPrioritaria').innerText = cPrio;
            }

            priorityHaloLayer.clearLayers();
            if (showPriorityHighlight) {
                featuresFiltradas.forEach(f => {
                    if (f.properties.is_priority) {
                        const coords = f.geometry.coordinates.map(pt => [pt[1], pt[0]]);
                        L.polyline(coords, {
                            pane: 'priorityHaloPane',
                            color: '#1D4ED8',
                            weight: 7.5,
                            opacity: 1.0,
                            lineCap: 'round',
                            lineJoin: 'round'
                        }).addTo(priorityHaloLayer);
                    }
                });
            }

            if (geojsonLayer) map.removeLayer(geojsonLayer);
            geojsonLayer = L.geoJSON({ type: 'FeatureCollection', features: featuresFiltradas }, {
                renderer: canvasRenderer,
                style: function(feat) {
                    const p = feat.properties;
                    const ehManual = (p.status === 'possible_manual') || (Boolean(p.is_manual) && p.status !== 'not_collected');
                    let cor = '#00A859';
                    if (ehPeriodo) {
                        cor = corPorPercentual(Number(p.pct_periodo ?? 0), ehManual);
                    } else {
                        if (p.status === 'not_collected') cor = '#E53935';
                        else if (ehManual) cor = '#0284C7';
                        else if (p.status === 'possible_gps_gap') cor = '#FBC02D';
                    }

                    let weight = (p.is_priority && showPriorityHighlight) ? 4.0 : (ehManual ? 3.6 : 3.0);
                    if (selectedFeature && selectedFeature.properties.id === p.id) {
                        cor = '#7C3AED';
                        weight = 6.0;
                    }
                    return { color: cor, weight, opacity: 1.0, lineCap: 'round', lineJoin: 'round' };
                },
                onEachFeature: function(feat, layer) {
                    const p = feat.properties;
                    const prioTxt = p.is_priority ? ' · 🌟 Prioritária' : '';
                    const perTxt = ehPeriodo ? `<br/>Cobertura: <b>${Number(p.pct_periodo ?? 0).toFixed(1)}%</b> (${p.dias_coletados || 0}/${p.dias_previstos || 0} dias)` : '';
                    const veicTxt = (p.vehicles && p.vehicles.length) ? `<br/>Veículos: ${escapeHtml(p.vehicles.join(', '))}` : '';
                    layer.bindTooltip(`<b>${escapeHtml(p.street_name)}</b> · ${escapeHtml(p.sector)}${prioTxt}${perTxt}${veicTxt}`, { sticky: true });

                    layer.on('click', function(e) {
                        if (isEditMode && !isReadOnlyMode) {
                            if (activeGisTool === 'draw') {
                                L.DomEvent.stopPropagation(e);
                                processarCliqueDesenho(e.latlng);
                                return;
                            }
                            selecionarSegmento(feat);
                            return;
                        }
                        const stMap = {
                            'collected': 'Coletada',
                            'not_collected': 'Não Coletada (Omissão)',
                            'possible_manual': 'Coleta Manual',
                            'possible_gps_gap': 'Possível Falha GPS'
                        };
                        const sitTxt = ehPeriodo
                            ? `${Number(p.pct_periodo ?? 0).toFixed(1)}% (${p.dias_coletados || 0}/${p.dias_previstos || 0} dias previstos)`
                            : (stMap[p.status] || p.status);
                        const popHtml = `
                            <div style="font-size:12px; min-width:200px; line-height:1.45;">
                                <div style="font-weight:800; color:#0F172A; border-bottom:1px solid #E2E8F0; padding-bottom:4px; margin-bottom:4px;">
                                    ${escapeHtml(p.street_name)}
                                </div>
                                <div><b>Setor:</b> ${escapeHtml(p.sector)} | <b>Extensão:</b> ${Number(p.len_m||0).toFixed(1)} m</div>
                                <div><b>Situação:</b> ${escapeHtml(sitTxt)}</div>
                                <div><b>Tipo:</b> ${escapeHtml(p.tipo || 'COLETA')}${p.is_priority ? ' · 🌟 Prioritária' : ''}</div>
                                <div><b>Veículos Inlog:</b> ${p.vehicles && p.vehicles.length ? escapeHtml(p.vehicles.join(', ')) : 'Nenhum no período'}</div>
                            </div>
                        `;
                        L.popup().setLatLng(e.latlng).setContent(popHtml).openOn(map);
                    });
                }
            }).addTo(map);

            if (selectedSectors.size === 1) {
                const sCod = Array.from(selectedSectors)[0];
                const sInfo = setoresList.find(s => s.codigo === sCod);
                if (sInfo && sInfo.centro && sInfo.centro[0] !== 0) map.setView(sInfo.centro, 14, { animate: true });
            } else if (selectedSectors.size > 1) {
                const pts = setoresList.filter(s => selectedSectors.has(s.codigo) && s.centro && s.centro[0] !== 0).map(s => s.centro);
                if (pts.length > 0) map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 15 });
            }
            renderizarRotulosSetores();
            atualizarControlesRodapeMapa(r, pct);
        }

        function atualizarControlesRodapeMapa(resumo, pctAtual) {
            const cidadeSel = document.getElementById('selectCidade')?.value || 'cabo';
            const btnPrint = document.getElementById('btnPrintMapaCidade');
            const legendAll = document.getElementById('allCitiesBottomLegend');

            if (btnPrint) {
                btnPrint.style.display = (cidadeSel !== 'all') ? 'inline-flex' : 'none';
            }
            if (legendAll) {
                const lista = (resumo && Array.isArray(resumo.resumos_cidades)) ? resumo.resumos_cidades : [];
                if (cidadeSel === 'all' && lista.length > 0) {
                    legendAll.innerHTML = '';
                    lista.forEach(c => {
                        const pctVal = Number(c.pct_cobertura || 0);
                        const cls = pctVal >= 90 ? 'pct-ok' : (pctVal >= 70 ? 'pct-mid' : 'pct-low');
                        const pill = document.createElement('button');
                        pill.type = 'button';
                        pill.className = 'city-pill';
                        pill.innerHTML = `<span>${escapeHtml(c.nome)}</span> <span class="${cls}">${pctVal.toFixed(1)}%</span>`;
                        pill.title = `Abrir ${c.nome}`;
                        pill.addEventListener('click', () => {
                            const sel = document.getElementById('selectCidade');
                            if (sel) {
                                sel.value = c.id;
                                carregarAuditoria(false);
                            }
                        });
                        legendAll.appendChild(pill);
                    });
                    legendAll.style.display = 'flex';
                } else {
                    legendAll.style.display = 'none';
                }
            }
        }

        async function capturarMapaECopiarTexto() {
            const cidade = document.getElementById('selectCidade')?.value || 'cabo';
            if (cidade === 'all') return;

            // 1. Enquadra corretamente o mapeamento da cidade (ruas encostadas na borda)
            if (geojsonLayer && geojsonLayer.getBounds && geojsonLayer.getBounds().isValid()) {
                map.fitBounds(geojsonLayer.getBounds(), { padding: [6, 6], animate: false });
            } else if (CIDADES_COORDENADAS[cidade]) {
                map.setView(CIDADES_COORDENADAS[cidade].centro, CIDADES_COORDENADAS[cidade].zoom, { animate: false });
            }

            // 2. Monta o texto formatado conforme horário e %
            const hora = new Date().getHours();
            const saudacao = hora < 12 ? 'Bom dia!' : (hora < 18 ? 'Boa tarde!' : 'Boa noite!');
            const dataIso = (currentAudit && currentAudit.resumo && currentAudit.resumo.data_iso) || document.getElementById('inputData')?.value || formatDataHojeISO();
            let dataFmt = dataIso;
            if (/^\d{4}-\d{2}-\d{2}$/.test(dataIso)) {
                const [a, m, d] = dataIso.split('-');
                dataFmt = `${d}/${m}/${a}`;
            }
            const kpiTxt = (document.getElementById('kpiCobertura')?.innerText || '').replace('%', '').trim();
            const pctNum = parseFloat(kpiTxt);
            const pctStr = !isNaN(pctNum) ? Math.round(pctNum) : Math.round(Number(currentAudit?.resumo?.pct_cobertura || 0));
            const texto = `${saudacao}\n\nSegue o Mapa de Execução da coleta do dia ${dataFmt}, com percentual de ${pctStr}% através do rastreamento do veículo.`;

            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(texto);
                } else {
                    const ta = document.createElement('textarea');
                    ta.value = texto;
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                }
            } catch (e) {
                console.warn('Erro ao copiar texto:', e);
            }

            // 3. Aguarda redesenho dos tiles/canvas após fitBounds e salva o print do mapa
            await new Promise(r => setTimeout(r, 450));
            try {
                if (typeof html2canvas === 'function') {
                    const mapEl = document.getElementById('map');
                    const canvas = await html2canvas(mapEl, {
                        useCORS: true,
                        allowTaint: false,
                        logging: false,
                        ignoreElements: el => el.classList && el.classList.contains('leaflet-control-zoom')
                    });
                    const link = document.createElement('a');
                    link.download = `mapa_execucao_${cidade}_${dataIso}.png`;
                    link.href = canvas.toDataURL('image/png');
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                }
                mostrarToast('Print salvo e texto copiado para o Ctrl+V!');
            } catch (err) {
                console.warn('Erro ao capturar imagem do mapa:', err);
                mostrarToast('Texto copiado para o Ctrl+V!');
            }
        }

        // ====================================================================
        // MODO EDIÇÃO GIS (QGIS WEB)
        // ====================================================================
        function toggleEditMode() {
            isEditMode = !isEditMode;
            document.getElementById('btnToggleEditMode').classList.toggle('active', isEditMode);
            document.getElementById('panelAuditoria').style.display = isEditMode ? 'none' : 'block';
            document.getElementById('panelEdicaoGIS').style.display = isEditMode ? 'block' : 'none';
            document.getElementById('gisToolbar').style.display = isEditMode ? 'flex' : 'none';
            if (isEditMode) setGisTool('select');
            else { cancelarDesenho(); aplicarFiltros(); }
        }

        function setGisTool(tool) {
            activeGisTool = tool;
            ['toolSelect', 'toolDrawLine', 'toolDelete'].forEach(id => {
                const b = document.getElementById(id);
                if (b) b.classList.remove('active');
            });
            const instrBox = document.getElementById('drawInstructionBox');
            if (tool === 'select') {
                document.getElementById('toolSelect').classList.add('active');
                document.getElementById('map').style.cursor = 'default';
                instrBox.style.display = 'none';
                cancelarDesenho();
            } else if (tool === 'draw') {
                document.getElementById('toolDrawLine').classList.add('active');
                document.getElementById('map').style.cursor = 'crosshair';
                iniciarModoDesenho();
            } else if (tool === 'delete') {
                document.getElementById('toolDelete').classList.add('active');
                excluirTrecho();
            }
        }

        function iniciarModoDesenho() {
            cancelarDesenho();
            document.getElementById('drawInstructionBox').style.display = 'flex';
            document.getElementById('drawInstructionText').innerHTML = '📍 <b>1º Clique:</b> Marque o INÍCIO da nova rua no mapa.';
            document.getElementById('editId').value = "NOVA RUA DO PROJETO";
            document.getElementById('editNomeRua').value = "";
            document.getElementById('editExtensao').innerText = "Aguardando traçado...";
        }

        function onMapClickForDrawing(e) {
            if (!isEditMode || activeGisTool !== 'draw') return;
            processarCliqueDesenho(e.latlng);
        }

        function processarCliqueDesenho(latlng) {
            const instrText = document.getElementById('drawInstructionText');
            if (drawingCoordinates.length === 0) {
                drawingCoordinates.push([latlng.lat, latlng.lng]);
                drawStartMarker = L.circleMarker([latlng.lat, latlng.lng], {
                    radius: 7, fillColor: '#00A859', color: '#FFFFFF', weight: 2, fillOpacity: 1
                }).addTo(map).bindTooltip("Início", { permanent: true, direction: 'top' });
                instrText.innerHTML = '📍 <b>Próximo Clique:</b> Adicione vértices ou marque o FIM da rua.';
            } else {
                drawingCoordinates.push([latlng.lat, latlng.lng]);
                if (drawGuidePolyline) { map.removeLayer(drawGuidePolyline); drawGuidePolyline = null; }
                if (drawActivePolyline) map.removeLayer(drawActivePolyline);
                drawActivePolyline = L.polyline(drawingCoordinates, { color: '#2563EB', weight: 5, dashArray: '6, 6' }).addTo(map);
                if (drawEndMarker) map.removeLayer(drawEndMarker);
                drawEndMarker = L.circleMarker([latlng.lat, latlng.lng], {
                    radius: 7, fillColor: '#E53935', color: '#FFFFFF', weight: 2, fillOpacity: 1
                }).addTo(map).bindTooltip("Fim", { permanent: true, direction: 'bottom' });
                document.getElementById('editExtensao').innerText = calcularDistanciaMetros(drawingCoordinates).toFixed(1) + ' metros';
                instrText.innerHTML = '✔ <b>Traçado pronto!</b> Preencha os atributos ao lado e clique em <b>Salvar no Projeto</b>.';
                document.getElementById('editNomeRua').focus();
            }
        }

        function onMapMouseMoveForDrawing(e) {
            if (!isEditMode || activeGisTool !== 'draw' || drawingCoordinates.length === 0) return;
            const coordsGuia = [drawingCoordinates[drawingCoordinates.length - 1], [e.latlng.lat, e.latlng.lng]];
            if (drawGuidePolyline) drawGuidePolyline.setLatLngs(coordsGuia);
            else drawGuidePolyline = L.polyline(coordsGuia, { color: '#2563EB', weight: 2.5, dashArray: '4, 6', opacity: 0.8 }).addTo(map);
        }

        function cancelarDesenho() {
            if (drawStartMarker) map.removeLayer(drawStartMarker);
            if (drawEndMarker) map.removeLayer(drawEndMarker);
            if (drawActivePolyline) map.removeLayer(drawActivePolyline);
            if (drawGuidePolyline) map.removeLayer(drawGuidePolyline);
            drawStartMarker = drawEndMarker = drawActivePolyline = drawGuidePolyline = null;
            drawingCoordinates = [];
            document.getElementById('drawInstructionBox').style.display = 'none';
        }

        function calcularDistanciaMetros(coords) {
            let total = 0.0;
            for (let i = 0; i < coords.length - 1; i++) {
                total += L.latLng(coords[i][0], coords[i][1]).distanceTo(L.latLng(coords[i+1][0], coords[i+1][1]));
            }
            return total;
        }

        function selecionarSegmento(feat) {
            selectedFeature = feat;
            const p = feat.properties;
            document.getElementById('editId').value = p.id;
            document.getElementById('editNomeRua').value = p.street_name;
            document.getElementById('editSetorInput').value = p.sector;
            document.getElementById('editTipo').value = p.tipo || (p.is_manual ? 'MANUAL' : 'COLETA');
            document.getElementById('editIsPriority').checked = p.is_priority || false;
            document.getElementById('editExtensao').innerText = p.len_m + ' metros';
            cancelarDesenho();
            aplicarFiltros();
        }

        async function salvarAlteracoesNoProjeto() {
            const cidade = document.getElementById('selectCidade').value;
            const idVal = document.getElementById('editId').value;
            const nomeRua = document.getElementById('editNomeRua').value.trim();
            const setor = document.getElementById('editSetorInput').value.trim() || 'S/S';
            const tipo = document.getElementById('editTipo').value;
            const isPriority = document.getElementById('editIsPriority').checked;

            if (!nomeRua) { alert('Informe o nome do logradouro / rua.'); document.getElementById('editNomeRua').focus(); return; }
            const payload = { contrato_id: cidade, nome_rua: nomeRua, setor, tipo, is_manual: (tipo === 'MANUAL'), is_priority: isPriority };
            const isNova = (idVal === "NOVA RUA DO PROJETO" || idVal === "Nenhum selecionado" || !idVal);

            if (!isNova) payload.id = parseInt(idVal);
            else {
                if (drawingCoordinates.length < 2) return alert('Clique em "Desenhar Rua" e trace o trecho no mapa.');
                payload.coordinates = drawingCoordinates.map(pt => [pt[1], pt[0]]);
            }

            document.getElementById('loadingOverlay').style.display = 'flex';
            document.getElementById('loadingText').innerText = 'Salvando trecho no SQLite e recalculando auditoria real...';
            try {
                const resp = await fetch('/api/segmento/salvar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
                });
                const res = await resp.json();
                if (res.sucesso) {
                    mostrarToast('Projeto atualizado no SQLite!');
                    cancelarDesenho();
                    setGisTool('select');
                    await carregarAuditoria(true);
                } else alert('Erro ao salvar: ' + (res.erro || 'Falha'));
            } catch (err) {
                console.error(err);
                alert('Erro de comunicação ao salvar.');
            } finally {
                document.getElementById('loadingOverlay').style.display = 'none';
            }
        }

        async function excluirTrecho() {
            const idVal = document.getElementById('editId').value;
            if (!idVal || idVal === "NOVA RUA DO PROJETO" || idVal === "Nenhum selecionado") {
                return alert('Selecione uma rua no mapa antes de apagar.');
            }
            if (!confirm(`Remover o trecho ID ${idVal} do Projeto da Cidade?`)) return;
            document.getElementById('loadingOverlay').style.display = 'flex';
            try {
                const resp = await fetch('/api/segmento/excluir', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: parseInt(idVal) })
                });
                const res = await resp.json();
                if (res.sucesso) {
                    mostrarToast('Trecho removido do projeto!');
                    selectedFeature = null;
                    document.getElementById('editId').value = "Nenhum selecionado";
                    await carregarAuditoria(true);
                }
            } catch (err) {
                console.error(err);
            } finally {
                document.getElementById('loadingOverlay').style.display = 'none';
            }
        }

        async function importarGeoJsonArquivo(file) {
            if (!file) return;
            const cidade = document.getElementById('selectCidade').value;
            const substituir = confirm(`Deseja SUBSTITUIR toda a malha existente de ${CIDADES_MAPA[cidade] || cidade}?\n\nOK = Substituir malha inteira\nCancelar = Adicionar ao projeto atual`);
            try {
                const texto = await file.text();
                const geojson = JSON.parse(texto);
                document.getElementById('loadingOverlay').style.display = 'flex';
                document.getElementById('loadingText').innerText = 'Importando malha GeoJSON para o SQLite...';
                const resp = await fetch('/api/malha/importar', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ contrato_id: cidade, geojson, substituir })
                });
                const res = await resp.json();
                if (res.sucesso) {
                    mostrarToast(`${res.importados} segmentos importados com sucesso!`);
                    await carregarAuditoria(true);
                } else {
                    alert('Erro na importação: ' + (res.erro || 'GeoJSON inválido'));
                }
            } catch (e) {
                alert('Erro ao ler arquivo GeoJSON: ' + e.message);
            } finally {
                document.getElementById('loadingOverlay').style.display = 'none';
                document.getElementById('inputImportGeoJson').value = '';
            }
        }

        // ====================================================================
        // CENTRAL INLOG (TODOS OS 10 ENDPOINTS)
        // ====================================================================
        function abrirCentralInlog(tab = 'frota') {
            const cidade = document.getElementById('selectCidade').value;
            const data = document.getElementById('inputData').value;
            document.getElementById('centralCidadeLabel').innerText = CIDADES_MAPA[cidade] || cidade;
            document.getElementById('centralDataLabel').innerText = data;
            document.getElementById('modalCentralInlog').style.display = 'flex';
            selecionarAbaCentral(tab, false);
        }

        function selecionarAbaCentral(tab, force = false) {
            activeCentralTab = tab;
            document.querySelectorAll('#centralTabsBar .tab-btn').forEach(b => {
                b.classList.toggle('active', b.dataset.tab === tab);
            });
            carregarConteudoAbaCentral(tab, force);
        }

        async function carregarConteudoAbaCentral(tab, force = false) {
            const body = document.getElementById('centralTabBody');
            const footer = document.getElementById('centralFooterInfo');
            const cidade = document.getElementById('selectCidade').value;
            const operacao = document.getElementById('selectOperacao').value;
            const data = document.getElementById('inputData').value;

            body.innerHTML = '<div style="padding:36px; text-align:center; color:#64748B;">⏳ Carregando dados oficiais da API Inlog...</div>';
            footer.innerText = '';

            try {
                if (tab === 'frota') {
                    const resp = await fetch(`/api/inlog/veiculos-tempo-real?cidade=${encodeURIComponent(cidade)}&operacao=${encodeURIComponent(operacao)}&force=${force}`);
                    const res = await resp.json();
                    const lista = res.veiculos || [];
                    footer.innerText = `Endpoints: GET /api/Veiculos/TempoReal + GET /api/Veiculos + GET /api/Motoristas (${lista.length} veículos)`;

                    body.innerHTML = `
                        <div class="tab-toolbar">
                            <input type="text" id="searchFrota" placeholder="🔍 Filtrar por placa, modelo, motorista ou grupo..." style="min-width:300px;" />
                            <div style="font-size:0.77rem; color:#475569;">
                                <b>${lista.length}</b> veículos na cidade ·
                                <span style="color:#166534;"><b>${lista.filter(v=>v.statusIgnicaoVeiculo==='L').length}</b> ignição ligada</span> ·
                                <span style="color:#1E40AF;"><b>${lista.filter(v=>v.statusConexaoAVL==='T').length}</b> AVL conectado</span>
                            </div>
                        </div>
                        <div class="data-table-wrap">
                            <table class="inlog-table" id="tblFrota">
                                <thead>
                                    <tr>
                                        <th>Placa</th><th>Tipo / Modelo</th><th>Motorista</th><th>Grupo Controle</th>
                                        <th>AVL</th><th>Ignição</th><th>Vel (km/h)</th><th>Sensores</th><th>Última Atualização</th><th>Ações</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${lista.map(v => {
                                        const cad = v.cadastro || {};
                                        const mod = [cad.fabricante, cad.modelo, cad.ano].filter(Boolean).join(' ');
                                        const mot = v.motoristaNome ? `${v.matriculaMotorista || ''} - ${v.motoristaNome}` : (v.matriculaMotorista || '—');
                                        const grps = (v.gruposControle || []).map(g => g.descricao).join(', ');
                                        return `<tr>
                                            <td><b>${escapeHtml(v.placaVeiculo)}</b></td>
                                            <td>${escapeHtml(v.tipoVeiculo)}<br/><span style="color:#64748B;font-size:0.7rem;">${escapeHtml(mod)}</span></td>
                                            <td>${escapeHtml(mot)}</td>
                                            <td>${escapeHtml(grps)}</td>
                                            <td><span class="pill ${v.statusConexaoAVL==='T'?'pill-green':'pill-red'}">${v.statusConexaoAVL==='T'?'Conectado':'Offline'}</span></td>
                                            <td><span class="pill ${v.statusIgnicaoVeiculo==='L'?'pill-blue':'pill-gray'}">${v.statusIgnicaoVeiculo==='L'?'Ligada':'Desligada'}</span></td>
                                            <td><b>${Number(v.velocidade||0).toFixed(0)}</b></td>
                                            <td>${escapeHtml(v.sensor1||'-')}/${escapeHtml(v.sensor2||'-')}/${escapeHtml(v.sensor3||'-')}</td>
                                            <td>${escapeHtml((v.dataHoraAtualizacao||'').replace('T',' '))}</td>
                                            <td style="white-space:nowrap;">
                                                <button class="btn-chip" onclick="focarPontoNoMapa(${v.latitude||0}, ${v.longitude||0}, decodeURIComponent('${encodeURIComponent(String(v.placaVeiculo||''))}'))">📍 Mapa</button>
                                                <button class="btn-chip" onclick="verRastroPlaca(decodeURIComponent('${encodeURIComponent(String(v.placaVeiculo||''))}'))">🛰️ Rastro</button>
                                            </td>
                                        </tr>`;
                                    }).join('')}
                                </tbody>
                            </table>
                        </div>
                    `;
                    document.getElementById('searchFrota').addEventListener('input', function() {
                        const q = this.value.toLowerCase();
                        document.querySelectorAll('#tblFrota tbody tr').forEach(tr => {
                            tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
                        });
                    });
                }
                else if (tab === 'comparacao') {
                    const lista = (currentAudit && currentAudit.planejado_vs_executado) ? currentAudit.planejado_vs_executado : [];
                    const comRota = lista.filter(x => x.tem_rota_planejada);
                    const totPlan = comRota.reduce((s, x) => s + (x.km_planejado || 0), 0);
                    const totExec = comRota.reduce((s, x) => s + (x.km_executado_planejado || 0), 0);
                    const pctGeral = totPlan > 0 ? (totExec / totPlan * 100) : 0;
                    footer.innerText = `Relação: Malha Viária (.gpkg) × Rotas Planejadas (.gpkg) × Setores/RD Inlog × GPS Real (${lista.length} veículos)`;

                    body.innerHTML = `
                        <div class="tab-toolbar">
                            <input type="text" id="searchComparacao" placeholder="🔍 Filtrar por placa, motorista ou setor Inlog..." style="min-width:290px;" />
                            <div style="font-size:0.78rem; color:#475569;">
                                <b>${comRota.length}</b> carros com rota planejada ·
                                Planejado: <b>${totPlan.toFixed(1)} km</b> ·
                                Executado na rota: <b style="color:#166534;">${totExec.toFixed(1)} km (${pctGeral.toFixed(1)}%)</b>
                            </div>
                        </div>
                        <div class="data-table-wrap">
                            <table class="inlog-table" id="tblComparacao">
                                <thead>
                                    <tr>
                                        <th>Veículo / Motorista</th>
                                        <th>Setor Inlog (Planejado)</th>
                                        <th>Rota .gpkg</th>
                                        <th>Km Planejado</th>
                                        <th>Executado na Rota</th>
                                        <th>Aderência</th>
                                        <th>Faltante / Fora Rota</th>
                                        <th>GPS / Situação</th>
                                        <th>Ação</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${lista.map(c => {
                                        const pct = Number(c.pct_aderencia || 0);
                                        const pillCls = !c.tem_rota_planejada ? 'pill-gray' : (pct >= 80 ? 'pill-green' : (pct >= 40 ? 'pill-amber' : 'pill-red'));
                                        const setsHtml = (c.setores_inlog && c.setores_inlog.length)
                                            ? c.setores_inlog.map(s => escapeHtml(s)).join('<br/>')
                                            : '<span style="color:#94A3B8;">Sem vínculo na Inlog</span>';
                                        const rotasHtml = (c.rotas_planejadas && c.rotas_planejadas.length)
                                            ? c.rotas_planejadas.map(r => `<span class="pill pill-blue">${escapeHtml(r)}</span>`).join(' ')
                                            : '<span style="color:#94A3B8;">—</span>';
                                        return `<tr>
                                            <td><b>${escapeHtml(c.placa)}</b><br/><span style="font-size:0.70rem; color:#64748B;">${escapeHtml(c.motorista || '')}</span></td>
                                            <td style="font-size:0.73rem; max-width:220px;">${setsHtml}</td>
                                            <td>${rotasHtml}</td>
                                            <td><b>${c.tem_rota_planejada ? c.km_planejado.toFixed(2) + ' km' : '—'}</b><br/><span style="font-size:0.69rem;color:#64748B;">${c.trechos_planejados || 0} trechos</span></td>
                                            <td><b style="color:#166534;">${c.km_executado_planejado.toFixed(2)} km</b><br/><span style="font-size:0.69rem;color:#64748B;">${c.trechos_executados || 0} trechos</span></td>
                                            <td><span class="pill ${pillCls}">${c.tem_rota_planejada ? pct.toFixed(1) + '%' : 'S/ Rota'}</span></td>
                                            <td style="font-size:0.73rem;">
                                                Falta: <b style="color:#DC2626;">${c.km_faltante.toFixed(2)} km</b><br/>
                                                Fora: <span style="color:#475569;">${c.km_fora_rota.toFixed(2)} km</span>
                                            </td>
                                            <td style="font-size:0.72rem;">
                                                <b>${c.pontos_gps || 0}</b> pts GPS<br/>
                                                <span style="color:#64748B;">${escapeHtml(c.situacao_inlog || '')}</span>
                                            </td>
                                            <td>
                                                <button class="btn-chip" onclick="compararPlacaNoMapa(decodeURIComponent('${encodeURIComponent(String(c.placa||''))}'))">🗺️ Comparar</button>
                                            </td>
                                        </tr>`;
                                    }).join('')}
                                </tbody>
                            </table>
                        </div>
                    `;
                    document.getElementById('searchComparacao').addEventListener('input', function() {
                        const q = this.value.toLowerCase();
                        document.querySelectorAll('#tblComparacao tbody tr').forEach(tr => {
                            tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
                        });
                    });
                }
                else if (tab === 'setores') {
                    const resp = await fetch(`/api/inlog/setores?cidade=${encodeURIComponent(cidade)}&force=${force}`);
                    const res = await resp.json();
                    const cad = res.cadastro || [];
                    const rt = res.tempo_real || [];
                    footer.innerText = `Endpoints: GET /api/Setores (${cad.length}) + GET /api/Setores/TempoReal (${rt.length})`;

                    body.innerHTML = `
                        <div class="tab-toolbar">
                            <input type="text" id="searchSetoresInlog" placeholder="🔍 Filtrar setor, macroSetor, placa ou logradouro..." style="min-width:300px;" />
                            <span style="font-size:0.75rem; color:#475569;">Nota: Sinótico de Setores pode levar até ~40s ao atualizar da Inlog (cache automático no SQLite).</span>
                        </div>
                        <h4 style="font-size:0.80rem; margin-bottom:6px;">Sinótico de Setores em Tempo Real (${rt.length})</h4>
                        <div class="data-table-wrap" style="max-height:300px; margin-bottom:14px;">
                            <table class="inlog-table" id="tblSetoresRT">
                                <thead>
                                    <tr><th>MacroSetor</th><th>Setor</th><th>Situação</th><th>Conclusão</th><th>Ocupação</th><th>Veículo</th><th>Turno</th><th>Logradouro Atual</th><th>Motorista</th></tr>
                                </thead>
                                <tbody>
                                    ${rt.length ? rt.map(s => `<tr>
                                        <td>${escapeHtml(s.macroSetor)}</td>
                                        <td><b>${escapeHtml(s.setor)}</b></td>
                                        <td><span class="pill ${s.situacao==='ATIVO'?'pill-green':(s.situacao==='EXECUTADO'?'pill-blue':'pill-gray')}">${escapeHtml(s.situacao)}</span></td>
                                        <td><b>${Number(s.percentualConclusao||0).toFixed(1)}%</b></td>
                                        <td>${Number(s.percentualOcupacao||0).toFixed(1)}%</td>
                                        <td>${escapeHtml(s.placaVeiculo||'—')}</td>
                                        <td>${escapeHtml(s.turno||'—')}</td>
                                        <td>${escapeHtml(s.logradouro||'—')}</td>
                                        <td>${escapeHtml(s.motorista||'—')}</td>
                                    </tr>`).join('') : '<tr><td colspan="9" style="text-align:center;color:#64748B;">Nenhum setor em tempo real encontrado para esta cidade. Clique em "Atualizar Aba da API" se desejar sincronizar agora.</td></tr>'}
                                </tbody>
                            </table>
                        </div>
                        <h4 style="font-size:0.80rem; margin-bottom:6px;">Cadastro Oficial de Setores Inlog (${cad.length})</h4>
                        <div class="data-table-wrap" style="max-height:240px;">
                            <table class="inlog-table" id="tblSetoresCad">
                                <thead><tr><th>ID</th><th>Apelido</th><th>Descrição</th><th>Tipo</th><th>Operação</th><th>Grupo Setor</th><th>MacroSetor</th></tr></thead>
                                <tbody>
                                    ${cad.length ? cad.map(s => `<tr>
                                        <td>${escapeHtml(s.identificador)}</td><td><b>${escapeHtml(s.apelido)}</b></td>
                                        <td>${escapeHtml(s.descricao)}</td><td>${escapeHtml(s.tipo)}</td>
                                        <td>${escapeHtml(s.operacao)}</td><td>${escapeHtml(s.grupoSetor)}</td><td>${escapeHtml(s.macroSetor)}</td>
                                    </tr>`).join('') : '<tr><td colspan="7" style="text-align:center;color:#64748B;">Nenhum setor cadastrado na API Inlog para esta cidade.</td></tr>'}
                                </tbody>
                            </table>
                        </div>
                    `;
                    document.getElementById('searchSetoresInlog').addEventListener('input', function() {
                        const q = this.value.toLowerCase();
                        document.querySelectorAll('#tblSetoresRT tbody tr, #tblSetoresCad tbody tr').forEach(tr => {
                            if (tr.children.length > 1) tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
                        });
                    });
                }
                else if (tab === 'rd') {
                    const resp = await fetch(`/api/inlog/relatorio-diario?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&force=${force}`);
                    const res = await resp.json();
                    const lista = res.registros || [];
                    window._rdCache = lista;
                    footer.innerText = `Endpoint: GET /api/RelatorioDiario (${lista.length} viagens/controles, ${Number(res.peso_total_kg||0).toLocaleString('pt-BR')} kg)`;

                    body.innerHTML = `
                        <div class="tab-toolbar">
                            <input type="text" id="searchRD" placeholder="🔍 Filtrar placa, setor, controle ou ticket..." style="min-width:280px;" />
                            <div style="font-size:0.78rem;">
                                Controles: <b>${lista.length}</b> · Peso Total Descarregado: <b style="color:var(--locar-green-dark);">${Number(res.peso_total_kg||0).toLocaleString('pt-BR')} kg</b>
                            </div>
                        </div>
                        <div class="data-table-wrap" style="max-height:330px;">
                            <table class="inlog-table" id="tblRD">
                                <thead><tr><th>Data Exec.</th><th>Grupo Controle</th><th>Setor</th><th>Placa</th><th>Controle</th><th>Tickets</th><th>Peso (kg)</th><th>Macros</th><th>Detalhe</th></tr></thead>
                                <tbody>
                                    ${lista.length ? lista.map((r, idx) => `<tr>
                                        <td>${escapeHtml((r.dataExecucao||'').slice(0,10))}</td>
                                        <td>${escapeHtml(r.grupoControle)}</td>
                                        <td>${escapeHtml(r.setor)}</td>
                                        <td><b>${escapeHtml(r.placa)}</b></td>
                                        <td style="font-family:monospace;font-size:0.72rem;">${escapeHtml(r.controle)}</td>
                                        <td>${escapeHtml((r.tickets||[]).join(', ') || '—')}</td>
                                        <td><b>${Number(r.pesoTotalKg||0).toLocaleString('pt-BR')}</b></td>
                                        <td>${(r.macros||[]).length}</td>
                                        <td><button class="btn-chip" onclick="verMacrosRD(${idx})">🔎 Ver Macros</button></td>
                                    </tr>`).join('') : '<tr><td colspan="9" style="text-align:center;color:#64748B;">Nenhum apontamento de Relatório Diário nesta data para a cidade.</td></tr>'}
                                </tbody>
                            </table>
                        </div>
                        <div id="rdMacrosBox" class="macros-drawer" style="display:none;"></div>
                    `;
                    document.getElementById('searchRD').addEventListener('input', function() {
                        const q = this.value.toLowerCase();
                        document.querySelectorAll('#tblRD tbody tr').forEach(tr => {
                            if (tr.children.length > 1) tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
                        });
                    });
                }
                else if (tab === 'eventos') {
                    const resp = await fetch(`/api/inlog/alarmes-falhas?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&force=${force}`);
                    const res = await resp.json();
                    const alarmes = res.alarmes || [];
                    const falhas = res.falhas || [];
                    footer.innerText = `Endpoints: GET /api/Alarmes (${alarmes.length}) + GET /api/Falha (${falhas.length})`;

                    body.innerHTML = `
                        <h4 style="font-size:0.80rem; margin-bottom:6px;">⚠️ Alarmes Registrados (${alarmes.length})</h4>
                        <div class="data-table-wrap" style="max-height:280px; margin-bottom:14px;">
                            <table class="inlog-table">
                                <thead><tr><th>Placa</th><th>Descrição do Alarme</th><th>Motorista</th><th>Início</th><th>Duração</th><th>Status</th><th>Mapa</th></tr></thead>
                                <tbody>
                                    ${alarmes.length ? alarmes.map(a => `<tr>
                                        <td><b>${escapeHtml(a.placa)}</b></td>
                                        <td>${escapeHtml(a.descricao)}</td>
                                        <td>${escapeHtml(a.motorista || '—')}</td>
                                        <td>${escapeHtml((a.dataHoraInicio||'').replace('T',' '))}</td>
                                        <td>${escapeHtml(a.duracao)}</td>
                                        <td><span class="pill pill-amber">${escapeHtml(a.status)}</span></td>
                                        <td><button class="btn-chip" onclick="focarPontoNoMapa(${a.latitude||0}, ${a.longitude||0}, decodeURIComponent('${encodeURIComponent(String(a.placa||'') + ' - ' + String(a.descricao||''))}'))">📍 Mapa</button></td>
                                    </tr>`).join('') : '<tr><td colspan="7" style="text-align:center;color:#64748B;">Nenhum alarme registrado para esta cidade na data.</td></tr>'}
                                </tbody>
                            </table>
                        </div>
                        <h4 style="font-size:0.80rem; margin-bottom:6px;">🔧 Falhas de Módulo (${falhas.length})</h4>
                        <div class="data-table-wrap" style="max-height:220px;">
                            <table class="inlog-table">
                                <thead><tr><th>Placa</th><th>Descrição da Falha</th><th>Módulo</th><th>Início</th><th>Duração</th><th>Hodômetro (m)</th><th>Mapa</th></tr></thead>
                                <tbody>
                                    ${falhas.length ? falhas.map(f => `<tr>
                                        <td><b>${escapeHtml(f.placa)}</b></td>
                                        <td>${escapeHtml(f.descricao)}</td>
                                        <td>${escapeHtml(f.descricaoFalhaModulo)}</td>
                                        <td>${escapeHtml((f.dataHoraInicio||'').replace('T',' '))}</td>
                                        <td>${escapeHtml(f.duracao)}</td>
                                        <td>${Number(f.hodometro||0).toLocaleString('pt-BR')}</td>
                                        <td><button class="btn-chip" onclick="focarPontoNoMapa(${f.latitude||0}, ${f.longitude||0}, decodeURIComponent('${encodeURIComponent(String(f.placa||'') + ' - ' + String(f.descricao||''))}'))">📍 Mapa</button></td>
                                    </tr>`).join('') : '<tr><td colspan="7" style="text-align:center;color:#64748B;">Nenhuma falha de módulo registrada para esta cidade na data.</td></tr>'}
                                </tbody>
                            </table>
                        </div>
                    `;
                }
                else if (tab === 'telemetria') {
                    const [respTel, respMot] = await Promise.all([
                        fetch(`/api/inlog/telemetria?cidade=${encodeURIComponent(cidade)}&data=${encodeURIComponent(data)}&force=${force}`),
                        fetch(`/api/inlog/motoristas?force=${force}`)
                    ]);
                    const resTel = await respTel.json();
                    const resMot = await respMot.json();
                    const resumoVeic = resTel.resumo_veiculos || [];
                    const motoristas = resMot.motoristas || [];
                    footer.innerText = `Endpoints: GET /api/Telemetria (${resTel.total_registros || 0} janelas) + GET /api/Motoristas (${motoristas.length} motoristas)`;

                    body.innerHTML = `
                        <h4 style="font-size:0.80rem; margin-bottom:6px;">🌿 Telemetria & Faixa Verde por Veículo (${resumoVeic.length})</h4>
                        <div class="data-table-wrap" style="max-height:280px; margin-bottom:14px;">
                            <table class="inlog-table">
                                <thead><tr><th>Placa</th><th>Janelas</th><th>Movimento (min)</th><th>Faixa Verde (min)</th><th>Disponível FV (min)</th><th>% Aproveitamento FV</th><th>Freio Pé</th><th>Freio Estac.</th></tr></thead>
                                <tbody>
                                    ${resumoVeic.length ? resumoVeic.map(t => `<tr>
                                        <td><b>${escapeHtml(t.placa)}</b></td>
                                        <td>${t.janelas}</td>
                                        <td>${(t.tempo_movimento_s / 60).toFixed(1)}</td>
                                        <td>${(t.tempo_faixa_verde_s / 60).toFixed(1)}</td>
                                        <td>${(t.tempo_disponivel_fv_s / 60).toFixed(1)}</td>
                                        <td><span class="pill ${t.pct_faixa_verde>=60?'pill-green':'pill-amber'}">${t.pct_faixa_verde.toFixed(1)}%</span></td>
                                        <td>${t.qtde_freio_pe}</td>
                                        <td>${t.qtde_freio_estac}</td>
                                    </tr>`).join('') : '<tr><td colspan="8" style="text-align:center;color:#64748B;">Sem dados de telemetria agregados para a cidade nesta data.</td></tr>'}
                                </tbody>
                            </table>
                        </div>
                        <div class="tab-toolbar" style="margin-top:8px;">
                            <h4 style="font-size:0.80rem; margin:0;">👷 Cadastro de Motoristas Inlog (${motoristas.length})</h4>
                            <input type="text" id="searchMotoristas" placeholder="🔍 Filtrar matrícula ou nome..." style="min-width:250px;" />
                        </div>
                        <div class="data-table-wrap" style="max-height:210px;">
                            <table class="inlog-table" id="tblMotoristas">
                                <thead><tr><th>Matrícula</th><th>Nome Completo</th></tr></thead>
                                <tbody>
                                    ${motoristas.slice(0, 400).map(m => `<tr><td><b>${escapeHtml(m.matricula)}</b></td><td>${escapeHtml(m.nome)}</td></tr>`).join('')}
                                </tbody>
                            </table>
                        </div>
                    `;
                    document.getElementById('searchMotoristas').addEventListener('input', function() {
                        const q = this.value.toLowerCase();
                        document.querySelectorAll('#tblMotoristas tbody tr').forEach(tr => {
                            tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
                        });
                    });
                }
                else if (tab === 'logs') {
                    const resp = await fetch(`/api/inlog/logs?_t=${Date.now()}`);
                    const res = await resp.json();
                    const logs = res.logs || [];
                    const stats = res.estatisticas_banco || {};
                    footer.innerText = `Auditoria de integridade HTTP · Banco SQLite local`;

                    body.innerHTML = `
                        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px;">
                            ${Object.entries(stats).map(([k, v]) => `<div class="pill pill-blue">${escapeHtml(k)}: <b>${Number(v).toLocaleString('pt-BR')}</b></div>`).join('')}
                        </div>
                        <div class="data-table-wrap">
                            <table class="inlog-table">
                                <thead><tr><th>Horário (UTC)</th><th>Endpoint Inlog</th><th>Parâmetros</th><th>HTTP</th><th>Status</th><th>Registros</th><th>Tempo</th><th>Mensagem</th></tr></thead>
                                <tbody>
                                    ${logs.map(l => `<tr>
                                        <td style="white-space:nowrap;">${escapeHtml(l.criado_em)}</td>
                                        <td><b>${escapeHtml(l.endpoint)}</b></td>
                                        <td style="font-family:monospace;font-size:0.71rem;max-width:220px;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(l.parametros)}</td>
                                        <td><b>${l.status_http}</b></td>
                                        <td><span class="pill ${l.sucesso?'pill-green':'pill-red'}">${l.sucesso?'OK':'FALHA'}</span></td>
                                        <td><b>${Number(l.qtd_registros||0).toLocaleString('pt-BR')}</b></td>
                                        <td>${l.duracao_ms} ms</td>
                                        <td style="font-size:0.72rem;color:#64748B;">${escapeHtml(l.mensagem||'—')}</td>
                                    </tr>`).join('')}
                                </tbody>
                            </table>
                        </div>
                    `;
                }
            } catch (err) {
                console.error(err);
                body.innerHTML = `<div style="padding:20px; color:#991B1B;">Erro ao carregar dados: ${escapeHtml(err.message)}</div>`;
            }
        }

        function verMacrosRD(idx) {
            const item = (window._rdCache || [])[idx];
            const box = document.getElementById('rdMacrosBox');
            if (!item || !box) return;
            const macros = item.macros || [];
            box.style.display = 'block';
            box.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <b>Apontamentos de Macro · ${escapeHtml(item.placa)} (${escapeHtml(item.setor)})</b>
                    <button class="btn-chip" onclick="document.getElementById('rdMacrosBox').style.display='none'">Fechar</button>
                </div>
                <div class="data-table-wrap" style="max-height:200px;">
                    <table class="inlog-table">
                        <thead><tr><th>Data/Hora</th><th>Campo</th><th>Valor</th><th>Hodômetro (m)</th><th>Horímetro (min)</th><th>Consumo</th><th>Distância (m)</th><th>Duração</th></tr></thead>
                        <tbody>
                            ${macros.map(m => `<tr>
                                <td>${escapeHtml((m.dataHora||'').replace('T',' '))}</td>
                                <td><b>${escapeHtml(m.campo)}</b></td>
                                <td>${escapeHtml(m.valor)}</td>
                                <td>${Number(m.hodometro||0).toLocaleString('pt-BR')}</td>
                                <td>${Number(m.horimetro||0).toLocaleString('pt-BR')}</td>
                                <td>${Number(m.consumo||0).toLocaleString('pt-BR')}</td>
                                <td>${Number(m.distancia||0).toLocaleString('pt-BR')}</td>
                                <td>${escapeHtml(m.duracao||'')}</td>
                            </tr>`).join('')}
                        </tbody>
                    </table>
                </div>
            `;
        }

        function focarPontoNoMapa(lat, lon, titulo) {
            if (!lat && !lon) { mostrarToast('Este registro não possui coordenada GPS válida.'); return; }
            document.getElementById('modalCentralInlog').style.display = 'none';
            map.setView([lat, lon], 17, { animate: true });
            L.popup().setLatLng([lat, lon]).setContent(`<b>${escapeHtml(titulo)}</b>`).openOn(map);
        }

        async function verRastroPlaca(placa) {
            document.getElementById('modalCentralInlog').style.display = 'none';
            document.getElementById('filterVeiculo').value = placa;
            document.getElementById('chkLayerRastroGPS').checked = true;
            aplicarFiltros();
            await carregarRastroGpsMapa();
            mostrarToast(`Exibindo rastro GPS real de ${placa}`);
        }

        async function compararPlacaNoMapa(placa) {
            document.getElementById('modalCentralInlog').style.display = 'none';
            document.getElementById('filterVeiculo').value = placa;
            document.getElementById('chkLayerRastroGPS').checked = true;
            const chkPlan = document.getElementById('chkLayerRotaPlanejada');
            if (chkPlan) chkPlan.checked = true;
            aplicarFiltros();
            await atualizarCamadasInlog(false);
            mostrarToast(`Comparando rota planejada x executada de ${placa}`);
        }

        // ====================================================================
        // VISÃO GERAL MULTI-CIDADES
        // ====================================================================
        async function abrirVisaoGeral() {
            const modal = document.getElementById('modalVisaoGeral');
            const body = document.getElementById('visaoGeralBody');
            const data = document.getElementById('inputData').value;
            modal.style.display = 'flex';
            body.innerHTML = '<div style="padding:32px; text-align:center; color:#64748B;">⏳ Consultando frota em tempo real de todas as cidades na Inlog...</div>';
            try {
                const resp = await fetch(`/api/inlog/visao-geral?data=${encodeURIComponent(data)}&_t=${Date.now()}`);
                const res = await resp.json();
                const cidades = res.cidades || [];
                body.innerHTML = `
                    <div style="margin-bottom:12px; font-size:0.80rem; color:#475569;">
                        Total Locar na Inlog: <b>${res.total_veiculos_frota || 0}</b> veículos ·
                        <span style="color:#166534;"><b>${res.total_ligados || 0}</b> ligados agora</span> ·
                        <span style="color:#1E40AF;"><b>${res.total_conectados || 0}</b> AVL conectados</span>
                    </div>
                    <div class="overview-grid">
                        ${cidades.map(c => `
                            <div class="city-overview-card">
                                <div class="city-overview-head">
                                    <span class="city-overview-name">${escapeHtml(c.nome)}</span>
                                    <span class="pill ${c.veiculos_ligados>0?'pill-green':'pill-gray'}">${c.veiculos_ligados}/${c.total_veiculos} ligados</span>
                                </div>
                                <div class="city-overview-metrics">
                                    <div>AVL Conectado: <b>${c.veiculos_conectados}</b></div>
                                    <div>Setores Inlog: <b>${c.setores_inlog}</b></div>
                                    <div>Malha Local: <b>${c.vias_malha > 0 ? c.vias_malha + ' vias' : 'Sem GeoJSON'}</b></div>
                                    <div>Cobertura (${escapeHtml(data)}): <b>${c.cobertura_pct !== null ? c.cobertura_pct + '%' : '—'}</b></div>
                                </div>
                                <button class="btn-locar" style="width:100%; justify-content:center; padding:5px;" onclick="irParaCidade('${escapeHtml(c.id)}')">
                                    Abrir Monitoramento
                                </button>
                            </div>
                        `).join('')}
                    </div>
                `;
            } catch (e) {
                body.innerHTML = `<div style="color:#991B1B;">Erro ao carregar visão geral: ${escapeHtml(e.message)}</div>`;
            }
        }

        function irParaCidade(cidadeId) {
            document.getElementById('modalVisaoGeral').style.display = 'none';
            const sel = document.getElementById('selectCidade');
            if (Array.from(sel.options).some(o => o.value === cidadeId)) {
                sel.value = cidadeId;
                const coord = CIDADES_COORDENADAS[cidadeId];
                if (coord && map) {
                    map.flyTo(coord.centro, coord.zoom, { duration: 1.2 });
                    lastCenteredCity = cidadeId;
                }
                carregarAuditoria(false);
            }
        }

        // ====================================================================
        // MODAL DE COMPARTILHAMENTO (LINK SEGURO ASSINADO COM CIDADE ÚNICA)
        // ====================================================================
        function abrirModalShare() {
            const modal = document.getElementById('modalShareLink');
            if (!modal) return;
            const modalSelectCidade = document.getElementById('modalShareCidade');
            const mainSelectCidade = document.getElementById('selectCidade');
            modalSelectCidade.innerHTML = '';
            Array.from(mainSelectCidade.options).forEach(opt => {
                if (opt.value === 'all') return;
                const o = document.createElement('option');
                o.value = opt.value; o.textContent = opt.textContent;
                if (opt.selected) o.selected = true;
                modalSelectCidade.appendChild(o);
            });
            if (!modalSelectCidade.value && modalSelectCidade.options.length > 0) {
                modalSelectCidade.selectedIndex = 0;
            }
            const dataBase = document.getElementById('inputData').value || formatDataHojeISO();
            document.getElementById('modalShareData').value = dataBase;
            const modalMes = document.getElementById('modalShareMes');
            if (modalMes) modalMes.value = dataBase.slice(0, 7);
            const modalPeriodo = document.getElementById('modalSharePeriodo');
            if (modalPeriodo) modalPeriodo.value = 'diario';
            atualizarCamposPeriodoShare();
            document.getElementById('modalShareTurno').value = document.getElementById('filterTurno')?.value || 'all';
            document.getElementById('modalShareColeta').value = document.getElementById('filterColeta').value;

            const setoresResumoEl = document.getElementById('modalShareSetoresResumo');
            const totalVis = getSetoresDoTurnoAtual().length;
            setoresResumoEl.innerText = (selectedSectors.size === 0 || selectedSectors.size >= totalVis)
                ? 'Todos os setores visíveis na cidade' : Array.from(selectedSectors).join(', ');
            modal.style.display = 'flex';
            gerarUrlShare();
        }

        function atualizarCamposPeriodoShare() {
            const periodo = document.getElementById('modalSharePeriodo')?.value || 'diario';
            const inpData = document.getElementById('modalShareData');
            const inpMes = document.getElementById('modalShareMes');
            const lbl = document.getElementById('lblModalShareData');
            const ehMes = periodo !== 'diario';
            if (inpData) inpData.style.display = ehMes ? 'none' : '';
            if (inpMes) inpMes.style.display = ehMes ? '' : 'none';
            if (lbl) lbl.innerText = ehMes ? 'Mês / Ano de Referência:' : 'Data da Operação:';
        }

        async function gerarUrlShare(forcar = false) {
            atualizarCamposPeriodoShare();
            const forcarRecriar = (forcar === true);
            const base = window.location.origin;
            const cidade = document.getElementById('modalShareCidade').value;
            const periodo = document.getElementById('modalSharePeriodo')?.value || 'diario';
            let data = document.getElementById('modalShareData').value;
            if (periodo !== 'diario') {
                const mesVal = document.getElementById('modalShareMes')?.value || (data ? data.slice(0, 7) : formatDataHojeISO().slice(0, 7));
                data = (periodo === 'quinzena2') ? `${mesVal}-16` : `${mesVal}-01`;
            }
            const turno = document.getElementById('modalShareTurno')?.value || 'all';
            const coleta = document.getElementById('modalShareColeta').value;
            const setores = (selectedSectors.size > 0 && selectedSectors.size < setoresList.length)
                ? Array.from(selectedSectors) : [];
            const inp = document.getElementById('inputGeneratedShareUrl');
            const stEl = document.getElementById('shareSnapshotStatus');
            const btnCopy = document.getElementById('btnCopyGeneratedUrl');

            if (inp) inp.value = '';
            if (inp) inp.placeholder = 'Gerando snapshot congelado (aguarde)...';
            if (stEl) stEl.innerHTML = '⏳ Processando e congelando mapa sem API externa no acesso...';
            if (btnCopy) btnCopy.disabled = true;

            try {
                const resp = await fetch('/api/compartilhar/gerar', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cidade, data, periodo, turno, coleta, setores, forcar: forcarRecriar })
                });
                const res = await resp.json();
                if (res && res.sucesso && res.path) {
                    const url = `${base}${res.path}`;
                    if (inp) inp.value = url;
                    if (stEl) {
                        const kb = res.tamanho_kb ? `${res.tamanho_kb} KB` : '';
                        const horaCong = (res.atualizado_em || '').replace('T', ' ').slice(0, 16);
                        if (!res.janela_fechada) {
                            stEl.innerHTML = `⚡ <b>Snapshot ${res.recriado ? 'recriado' : 'gerado'}</b> (${kb}) · 🟡 <b>Dia em andamento</b> — gere novamente ou clique em Atualizar para renovar os dados · Sem expiração`;
                        } else if (res.reutilizado) {
                            stEl.innerHTML = `⚡ <b>Snapshot em cache</b> (${kb}) · 🔒 Janela fechada (${horaCong}) · Sem expiração`;
                        } else {
                            stEl.innerHTML = `⚡ <b>Snapshot congelado</b> (${kb}) · 🔒 Janela fechada (${horaCong}) · Sem expiração`;
                        }
                    }
                    return url;
                }
                if (stEl) stEl.innerHTML = `<span style="color:#DC2626;">Erro: ${escapeHtml((res && res.erro) || 'Falha ao gerar snapshot.')}</span>`;
            } catch (e) {
                console.error('Erro ao gerar snapshot:', e);
                if (stEl) stEl.innerHTML = '<span style="color:#DC2626;">Erro de comunicação ao gerar snapshot.</span>';
            } finally {
                if (btnCopy) btnCopy.disabled = false;
            }
            return '';
        }

        function exportarCsvClientSide() {
            const dataIso = (currentAudit && currentAudit.resumo && currentAudit.resumo.data_iso) || document.getElementById('inputData').value;
            const rows = [
                ['CIDADE', 'ID_VIA', 'NOME_RUA', 'BAIRRO', 'SETOR', 'STATUS_COLETA', 'EXTENSAO_METROS', 'VEICULOS_RESPONSAVEIS']
            ];
            (currentFeatures || []).forEach(f => {
                const p = f.properties || {};
                rows.push([
                    `"${String(p.cidade_nome || p.cidade_id || '').replace(/"/g, '""')}"`,
                    p.id || '',
                    `"${String(p.street_name || '').replace(/"/g, '""')}"`,
                    `"${String(p.bairro || '').replace(/"/g, '""')}"`,
                    `"${String(p.sector || '').replace(/"/g, '""')}"`,
                    p.status || '',
                    (p.len_m || 0).toFixed(2),
                    `"${(p.vehicles || []).join(';')}"`
                ]);
            });
            const csvContent = '\uFEFF' + rows.map(r => r.join(';')).join('\r\n');
            const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.setAttribute('download', `auditoria_todas_cidades_${dataIso}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            mostrarToast('CSV de todas as cidades baixado com sucesso!');
        }

        function baixarCsvAuditoria() {
            if (sharedSnapshotId) {
                window.location.href = `/api/snapshot/${encodeURIComponent(sharedSnapshotId)}/csv`;
                return;
            }
            if (!currentAudit || !currentAudit.resumo) {
                return mostrarToast('Nenhuma auditoria disponível para exportar CSV.');
            }
            if (currentAudit.resumo.contrato_id === 'all' || !currentAudit.resumo.id) {
                exportarCsvClientSide();
                return;
            }
            let csvUrl = `/api/exportar-csv?id=${currentAudit.resumo.id}`;
            if (sharedToken) csvUrl += `&token=${encodeURIComponent(sharedToken)}`;
            window.location.href = csvUrl;
        }

        // ====================================================================
        // BINDINGS DE EVENTOS
        // ====================================================================
        document.getElementById('btnToggleEditMode').addEventListener('click', toggleEditMode);
        document.getElementById('toolSelect').addEventListener('click', () => setGisTool('select'));
        document.getElementById('toolDrawLine').addEventListener('click', () => setGisTool('draw'));
        document.getElementById('toolDelete').addEventListener('click', () => setGisTool('delete'));
        document.getElementById('btnCancelDrawing').addEventListener('click', () => setGisTool('select'));
        document.getElementById('btnSalvarTrecho').addEventListener('click', salvarAlteracoesNoProjeto);
        document.getElementById('btnExcluirTrecho').addEventListener('click', excluirTrecho);

        document.getElementById('toolExportGeoJson').addEventListener('click', () => {
            const cidade = document.getElementById('selectCidade').value;
            window.location.href = `/api/exportar-geojson?cidade=${encodeURIComponent(cidade)}`;
        });
        document.getElementById('toolImportGeoJson').addEventListener('click', () => {
            document.getElementById('inputImportGeoJson').click();
        });
        document.getElementById('inputImportGeoJson').addEventListener('change', (e) => {
            importarGeoJsonArquivo(e.target.files[0]);
        });

        document.getElementById('filterColeta').addEventListener('change', () => {
            const fc = document.getElementById('filterColeta').value;
            const selOp = document.getElementById('selectOperacao');
            if (selOp && !isSharedLinkMode) {
                if (fc === 'domiciliar') selOp.value = 'DOMICILIAR';
                else if (fc === 'possible_manual') selOp.value = 'MANUAL';
                else if (fc === 'all') selOp.value = 'COLETA';
            }
            aplicarFiltros();
        });

        function sincronizarFiltroTurno(novoTurno) {
            const st = document.getElementById('selectTurno');
            const ft = document.getElementById('filterTurno');
            if (st && st.value !== novoTurno) st.value = novoTurno;
            if (ft && ft.value !== novoTurno) ft.value = novoTurno;
            selectedSectors.clear();
            renderizarListaSetoresFiltro();
            aplicarFiltros();
        }
        document.getElementById('filterTurno').addEventListener('change', (e) => sincronizarFiltroTurno(e.target.value));
        document.getElementById('selectTurno')?.addEventListener('change', (e) => sincronizarFiltroTurno(e.target.value));

        document.getElementById('filterVeiculo').addEventListener('change', () => {
            const v = document.getElementById('filterVeiculo').value;
            if (v !== 'all') {
                const chkPlan = document.getElementById('chkLayerRotaPlanejada');
                if (chkPlan && !isSharedLinkMode) chkPlan.checked = true;
            }
            aplicarFiltros();
            atualizarCamadasInlog(false);
        });

        document.getElementById('chkLayerVeiculosRT').addEventListener('change', () => atualizarCamadasInlog(false));
        document.getElementById('chkLayerRastroGPS').addEventListener('change', () => atualizarCamadasInlog(false));
        document.getElementById('chkLayerEventos').addEventListener('change', () => atualizarCamadasInlog(false));
        document.getElementById('chkLayerRotaPlanejada')?.addEventListener('change', () => atualizarCamadasInlog(false));
        document.getElementById('chkLayerMalhaBase')?.addEventListener('change', () => atualizarCamadasInlog(false));
        document.getElementById('chkAutoRefreshRT').addEventListener('change', function() {
            clearInterval(autoRefreshTimer);
            if (this.checked && !isSharedLinkMode) {
                autoRefreshTimer = setInterval(() => {
                    if (document.getElementById('chkLayerVeiculosRT').checked) carregarVeiculosTempoRealMapa(true);
                }, 30000);
                mostrarToast('Auto-atualização da frota ativada (30s)');
            }
        });

        document.getElementById('btnShowLabels').addEventListener('click', function() {
            showSectorLabels = true; this.classList.add('active');
            document.getElementById('btnHideLabels').classList.remove('active');
            renderizarRotulosSetores();
        });
        document.getElementById('btnHideLabels').addEventListener('click', function() {
            showSectorLabels = false; this.classList.add('active');
            document.getElementById('btnShowLabels').classList.remove('active');
            renderizarRotulosSetores();
        });
        document.getElementById('btnShowPriority').addEventListener('click', function() {
            showPriorityHighlight = true; this.classList.add('active');
            document.getElementById('btnHidePriority').classList.remove('active');
            aplicarFiltros();
        });
        document.getElementById('btnHidePriority').addEventListener('click', function() {
            showPriorityHighlight = false; this.classList.add('active');
            document.getElementById('btnShowPriority').classList.remove('active');
            aplicarFiltros();
        });

        document.getElementById('btnConsultar').addEventListener('click', () => carregarAuditoria(true));
        document.getElementById('selectCidade').addEventListener('change', (e) => {
            const cid = e.target.value;
            if (!isSharedLinkMode) selectedSectors.clear();
            const coord = CIDADES_COORDENADAS[cid];
            if (coord && map) {
                map.flyTo(coord.centro, coord.zoom, { duration: 1.2 });
                lastCenteredCity = cid;
            }
            carregarAuditoria(false);
        });
        document.getElementById('selectOperacao').addEventListener('change', (e) => {
            const val = e.target.value;
            const fc = document.getElementById('filterColeta');
            if (val === 'DOMICILIAR') fc.value = 'domiciliar';
            else if (val === 'MANUAL') fc.value = 'possible_manual';
            else fc.value = 'all';
            if (currentAudit) aplicarFiltros();
            else carregarAuditoria(false);
        });
        document.getElementById('inputData').addEventListener('change', () => carregarAuditoria(false));
        document.getElementById('btnBaixarCsv').addEventListener('click', baixarCsvAuditoria);
        document.getElementById('btnBaixarCsvLink')?.addEventListener('click', baixarCsvAuditoria);

        // Multi-Select Setores
        document.getElementById('btnMultiSetorTrigger').addEventListener('click', (e) => { e.stopPropagation(); toggleMultiSetorDropdown(); });
        document.getElementById('btnSelectAllSectors').addEventListener('click', (e) => { e.stopPropagation(); selectAllSectors(); });
        document.getElementById('btnClearSectors').addEventListener('click', (e) => { e.stopPropagation(); clearSectors(); });
        document.getElementById('inputSearchSector').addEventListener('input', function(e) { e.stopPropagation(); filtrarItensSetor(this.value); });
        document.getElementById('inputSearchSector').addEventListener('keydown', (e) => e.stopPropagation());
        document.addEventListener('click', (e) => {
            const c = document.getElementById('multiSetorContainer');
            if (c && !c.contains(e.target)) toggleMultiSetorDropdown(true);
        });

        // Modais Central Inlog & Visão Geral & Share
        document.getElementById('btnPlanejadoVsExec').addEventListener('click', () => abrirCentralInlog('comparacao'));
        document.getElementById('btnCentralInlog').addEventListener('click', () => abrirCentralInlog(activeCentralTab));
        document.getElementById('btnCloseCentralInlog').addEventListener('click', () => document.getElementById('modalCentralInlog').style.display = 'none');
        document.getElementById('btnFecharCentralInlog').addEventListener('click', () => document.getElementById('modalCentralInlog').style.display = 'none');
        document.getElementById('btnRefreshCentralTab').addEventListener('click', () => carregarConteudoAbaCentral(activeCentralTab, true));
        document.querySelectorAll('#centralTabsBar .tab-btn').forEach(btn => {
            btn.addEventListener('click', () => selecionarAbaCentral(btn.dataset.tab, false));
        });

        document.getElementById('btnVisaoGeral').addEventListener('click', abrirVisaoGeral);
        document.getElementById('btnCloseVisaoGeral').addEventListener('click', () => document.getElementById('modalVisaoGeral').style.display = 'none');
        document.getElementById('btnFecharVisaoGeral').addEventListener('click', () => document.getElementById('modalVisaoGeral').style.display = 'none');
        document.getElementById('btnRefreshVisaoGeral').addEventListener('click', abrirVisaoGeral);

        document.getElementById('btnShareLink').addEventListener('click', abrirModalShare);
        document.getElementById('btnPrintMapaCidade')?.addEventListener('click', capturarMapaECopiarTexto);
        document.getElementById('btnCloseModalShare').addEventListener('click', () => document.getElementById('modalShareLink').style.display = 'none');
        document.getElementById('btnCancelShareModal').addEventListener('click', () => document.getElementById('modalShareLink').style.display = 'none');
        ['modalShareCidade','modalShareData','modalShareMes','modalSharePeriodo','modalShareTurno','modalShareColeta'].forEach(id => {
            document.getElementById(id)?.addEventListener('change', () => gerarUrlShare(false));
        });
        document.getElementById('btnForcarRecriarShare')?.addEventListener('click', () => gerarUrlShare(true));
        document.getElementById('btnCopyGeneratedUrl').addEventListener('click', async () => {
            let url = document.getElementById('inputGeneratedShareUrl').value;
            if (!url) url = await gerarUrlShare(false);
            if (!url) return;
            navigator.clipboard.writeText(url).then(() => mostrarToast('Link rápido copiado!')).catch(() => {
                const inp = document.getElementById('inputGeneratedShareUrl'); inp.select(); document.execCommand('copy');
                mostrarToast('Link rápido copiado!');
            });
        });
        document.getElementById('btnTestOpenShareUrl').addEventListener('click', async () => {
            let url = document.getElementById('inputGeneratedShareUrl').value;
            if (!url) url = await gerarUrlShare(false);
            if (url) window.open(url, '_blank');
        });

        // Mobile Sidebar Drawer Toggle
        const btnToggleSidebarMobile = document.getElementById('btnToggleSidebarMobile');
        const sidebarRight = document.getElementById('sidebarRight');
        const sidebarMobileHeader = document.getElementById('sidebarMobileHeader');
        const sidebarMobileIcon = document.getElementById('sidebarMobileIcon');

        function alternarPainelMobile() {
            if (!sidebarRight) return;
            const recolhido = sidebarRight.classList.toggle('mobile-collapsed');
            if (sidebarMobileIcon) sidebarMobileIcon.innerText = recolhido ? '▲' : '▼';
            if (map) setTimeout(() => map.invalidateSize(), 260);
        }

        if (btnToggleSidebarMobile) {
            btnToggleSidebarMobile.addEventListener('click', alternarPainelMobile);
        }
        if (sidebarMobileHeader) {
            sidebarMobileHeader.addEventListener('click', alternarPainelMobile);
        }

        window.onload = () => {
            checkUrlParams();
            initMap();
            if (window.innerWidth <= 768 && sidebarRight) {
                sidebarRight.classList.add('mobile-collapsed');
                if (sidebarMobileIcon) sidebarMobileIcon.innerText = '▲';
            }
            if (map) setTimeout(() => map.invalidateSize(), 250);
            carregarAuditoria(false);
        };
    </script>
</body>
</html>
"""


class LocarRequestHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, raw: bytes, content_type: str, status: int = 200, extra_headers: Optional[Dict[str, str]] = None, cookies_out: Optional[list] = None):
        accept_enc = self.headers.get("Accept-Encoding", "").lower()
        usar_gzip = ("gzip" in accept_enc) and len(raw) > 1024
        body = gzip.compress(raw, compresslevel=4) if usar_gzip else raw

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        if usar_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        if cookies_out:
            for ck in cookies_out:
                self.send_header("Set-Cookie", ck)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: object, status: int = 200, extra_headers: Optional[Dict[str, str]] = None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Access-Control-Allow-Origin": "*"}
        if extra_headers:
            headers.update(extra_headers)
        self._send_bytes(raw, "application/json; charset=utf-8", status=status, extra_headers=headers)

    def _send_html(self, html_bytes: bytes, status: int = 200, cookies_out: Optional[list] = None, extra_headers: Optional[Dict[str, str]] = None):
        hdrs = {"Pragma": "no-cache", "Expires": "0"}
        if extra_headers:
            hdrs.update(extra_headers)
        self._send_bytes(
            html_bytes,
            "text/html; charset=utf-8",
            status=status,
            extra_headers=hdrs,
            cookies_out=cookies_out,
        )

    def _redirect(self, location: str, cookies_out: Optional[list] = None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if cookies_out:
            for ck in cookies_out:
                self.send_header("Set-Cookie", ck)
        self.end_headers()

    def _obter_cookies(self) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        raw = self.headers.get("Cookie", "")
        if not raw:
            return cookies
        sc = SimpleCookie()
        try:
            sc.load(raw)
            for k, m in sc.items():
                cookies[k] = m.value
        except Exception:
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    cookies[k] = v
        return cookies

    def _obter_sessao(self) -> tuple:
        cookies = self._obter_cookies()
        is_admin = validar_cookie_admin(cookies.get("lc_admin_auth"))
        lock_token = cookies.get("lc_viewer_lock")
        viewer_lock = None
        if lock_token:
            if "." in lock_token:
                viewer_lock = validar_token_compartilhamento(lock_token)
            else:
                snap_meta = snapshots.obter_snapshot_meta(lock_token)
                if snap_meta:
                    viewer_lock = {"snapshot_id": snap_meta["id"], **snap_meta["params"]}
        return is_admin, viewer_lock, lock_token

    def _is_https_request(self) -> bool:
        proto = self.headers.get("X-Forwarded-Proto", "").lower()
        return proto == "https"

    def _render_html_com_lock(self, shared_lock: Dict[str, Any]) -> bytes:
        script_tag = f"<script>window.__SHARED_LOCK__ = {json.dumps(shared_lock, ensure_ascii=False)};</script>"
        return HTML_PAGE.replace("<!-- __SHARED_LOCK_INIT__ -->", script_tag).encode("utf-8")

    def log_message(self, format, *args):
        # Log conciso das requisições no console operacional
        print(f"[HTTP] {self.address_string()} - {format % args}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/static/"):
            static_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
            rel_path = urllib.parse.unquote(path[len("/static/"):]).lstrip("/\\")
            alvo_path = os.path.abspath(os.path.join(static_root, rel_path))
            if (alvo_path == static_root or alvo_path.startswith(static_root + os.sep)) and os.path.isfile(alvo_path):
                ext = os.path.splitext(alvo_path)[1].lower()
                ctype_map = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".svg": "image/svg+xml",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".woff2": "font/woff2",
                    ".json": "application/json; charset=utf-8",
                }
                ctype = ctype_map.get(ext, "application/octet-stream")
                with open(alvo_path, "rb") as f_st:
                    st_bytes = f_st.read()
                if ext in (".css", ".js", ".svg", ".json") and "gzip" in self.headers.get("Accept-Encoding", "").lower() and len(st_bytes) > 1024:
                    st_bytes = gzip.compress(st_bytes, compresslevel=5)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(st_bytes)))
                self.end_headers()
                self.wfile.write(st_bytes)
            else:
                self.send_error(404)
            return

        is_admin, viewer_lock, lock_token_raw = self._obter_sessao()

        # 0. Rota Exclusiva do Link Compartilhado (/v/<snapshot_id> ou legado /v/<token>)
        if path.startswith("/v/"):
            token_ou_id = urllib.parse.unquote(path[3:].strip("/"))
            sec_headers = {
                "X-Robots-Tag": "noindex, nofollow",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-Content-Type-Options": "nosniff",
            }
            secure_attr = "; Secure" if self._is_https_request() else ""

            if "." in token_ou_id:
                payload = validar_token_compartilhamento(token_ou_id)
                if not payload:
                    erro_html = (
                        "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='utf-8'/>"
                        "<title>Link Inválido - LOCAR</title></head>"
                        "<body style='font-family:Inter,sans-serif;background:#F8FAFC;display:flex;align-items:center;justify-content:center;height:100vh;'>"
                        "<div style='background:#fff;padding:28px 34px;border-radius:10px;border-top:4px solid #E53935;box-shadow:0 8px 24px rgba(0,0,0,0.08);text-align:center;max-width:420px;'>"
                        "<h2 style='margin:0 0 8px;color:#1E293B;'>🔒 Link de Visualização Inválido</h2>"
                        "<p style='margin:0;color:#64748B;font-size:14px;'>Este link foi adulterado ou não é válido. Solicite um novo link oficial à equipe Locar.</p>"
                        "</div></body></html>"
                    ).encode("utf-8")
                    self._send_html(erro_html, status=403, extra_headers=sec_headers)
                    return

                lock_cfg = {"active": True, "token": token_ou_id, **payload}
                cookie_header = f"lc_viewer_lock={token_ou_id}; Path=/; HttpOnly; SameSite=Lax{secure_attr}"
                self._send_html(self._render_html_com_lock(lock_cfg), cookies_out=[cookie_header], extra_headers=sec_headers)
                return

            snap_meta = snapshots.obter_snapshot_meta(token_ou_id)
            if not snap_meta:
                erro_html = (
                    "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='utf-8'/>"
                    "<title>Link Indisponível - LOCAR</title></head>"
                    "<body style='font-family:Inter,sans-serif;background:#F8FAFC;display:flex;align-items:center;justify-content:center;height:100vh;'>"
                    "<div style='background:#fff;padding:28px 34px;border-radius:10px;border-top:4px solid #E53935;box-shadow:0 8px 24px rgba(0,0,0,0.08);text-align:center;max-width:420px;'>"
                    "<h2 style='margin:0 0 8px;color:#1E293B;'>🔒 Link Indisponível</h2>"
                    "<p style='margin:0;color:#64748B;font-size:14px;'>Este link não existe, expirou ou foi revogado pela equipe Locar.</p>"
                    "</div></body></html>"
                ).encode("utf-8")
                self._send_html(erro_html, status=404, extra_headers=sec_headers)
                return

            lock_cfg = {
                "active": True,
                "snapshot_id": snap_meta["id"],
                "congelado_em": snap_meta["atualizado_em"],
                "janela_fechada": snap_meta["janela_fechada"],
                **snap_meta["params"],
            }
            cookie_header = f"lc_viewer_lock={snap_meta['id']}; Path=/; HttpOnly; SameSite=Lax{secure_attr}"
            self._send_html(self._render_html_com_lock(lock_cfg), cookies_out=[cookie_header], extra_headers=sec_headers)
            return

        # 0a. Servir Snapshot Congelado (zero API Inlog, zero engine, GZIP pré-comprimido)
        if path.startswith("/api/snapshot/"):
            sub_parts = path[len("/api/snapshot/"):].strip("/").split("/")
            snap_id = urllib.parse.unquote(sub_parts[0]) if sub_parts else ""
            if len(sub_parts) == 2 and sub_parts[1] == "csv":
                csv_res = snapshots.obter_snapshot_csv(snap_id)
                if not csv_res:
                    self._send_json({"erro": "CSV do snapshot não encontrado ou revogado."}, status=404)
                    return
                csv_gz, csv_filename = csv_res
                accept_enc = self.headers.get("Accept-Encoding", "").lower()
                if "gzip" in accept_enc:
                    body_csv = csv_gz
                    usar_gz = True
                else:
                    body_csv = gzip.decompress(csv_gz)
                    usar_gz = False
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
                self.send_header("Content-Disposition", f'attachment; filename="{csv_filename}"')
                self.send_header("Cache-Control", "private, no-cache")
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("X-Content-Type-Options", "nosniff")
                if usar_gz:
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                self.send_header("Content-Length", str(len(body_csv)))
                self.end_headers()
                self.wfile.write(body_csv)
                return

            if len(sub_parts) == 1 and snap_id:
                snap = snapshots.obter_snapshot_payload(snap_id)
                if not snap:
                    self._send_json({"erro": "Snapshot não encontrado ou revogado."}, status=404)
                    return
                etag = f'"{snap["sha256"]}"'
                if_none_match = self.headers.get("If-None-Match", "").strip()
                if if_none_match == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "private, no-cache")
                    self.end_headers()
                    return

                accept_enc = self.headers.get("Accept-Encoding", "").lower()
                if "gzip" in accept_enc:
                    body_snap = snap["payload_gzip"]
                    usar_gz = True
                else:
                    body_snap = gzip.decompress(snap["payload_gzip"])
                    usar_gz = False

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, no-cache")
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("X-Content-Type-Options", "nosniff")
                if usar_gz:
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                self.send_header("Content-Length", str(len(body_snap)))
                self.end_headers()
                self.wfile.write(body_snap)
                return

        # 0b. Login Administrativo / Desbloqueio do Painel Central
        if path == "/login":
            locked = query.get("locked", ["0"])[0] == "1" or (viewer_lock is not None)
            notice = (
                "<div class='alert'>🔒 <b>Acesso ao Painel Central Bloqueado:</b> Este navegador está vinculado a um link exclusivo de cliente ou requer autenticação administrativa.</div>"
                if locked else
                "<div class='alert'>🔐 Informe a chave de acesso para gerenciar todas as cidades e gerar links.</div>"
            )
            back_btn = (
                f"<a class='back-btn' href='/v/{urllib.parse.quote(lock_token_raw)}'>⬅ Voltar para Visualização Compartilhada</a>"
                if (viewer_lock and lock_token_raw) else ""
            )
            html_login = LOGIN_PAGE.replace("__LOCKED_NOTICE__", notice).replace("__BACK_TO_LINK__", back_btn).encode("utf-8")
            self._send_html(html_login)
            return

        if path == "/logout":
            self._redirect("/login", cookies_out=[
                "lc_admin_auth=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            ])
            return

        # 1. Interface Web Principal SPA (Painel Central Protegido)
        if path in ("/", "/index.html"):
            if (not is_admin) and (viewer_lock is not None or REQUIRE_ADMIN_LOGIN):
                alvo = "/login?locked=1" if viewer_lock is not None else "/login"
                self._redirect(alvo)
                return
            self._send_html(self._render_html_com_lock({"active": False}))
            return

        # 2. Auditoria Espacial Real (com trava de cidade em link compartilhado)
        if path == "/api/auditoria":
            token_param = query.get("token", [None])[0]
            tok_payload = None
            if token_param:
                tok_payload = validar_token_compartilhamento(token_param)
                if not tok_payload:
                    self._send_json({"erro": "Token de visualização inválido."}, status=403)
                    return
            elif (not is_admin) and viewer_lock is not None:
                tok_payload = viewer_lock
            elif (not is_admin) and REQUIRE_ADMIN_LOGIN:
                self._send_json({"erro": "Autenticação administrativa necessária."}, status=401)
                return

            cidade = tok_payload["cidade"] if tok_payload else query.get("cidade", ["cabo"])[0]
            data_iso = (tok_payload.get("data") or query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]) if tok_payload else query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            periodo = str(tok_payload.get("periodo") or "diario").lower().strip() if tok_payload else str(query.get("periodo", ["diario"])[0]).lower().strip()
            operacao = query.get("operacao", ["COLETA"])[0]
            force = False if tok_payload else (query.get("force", ["false"])[0].lower() == "true")
            if periodo in ("quinzena1", "quinzena2", "mensal"):
                resultado = engine.processar_auditoria_periodo(
                    cidade, data_iso, periodo=periodo, operacao=operacao
                )
            else:
                resultado = engine.processar_ou_obter_auditoria(
                    cidade, data_iso, forcar_reprocessamento=force, operacao=operacao
                )
            self._send_json(resultado)
            return

        # 3. Exportação CSV de Vias Omitidas (permitida no Painel Central e no Link Assinado da mesma cidade)
        if path == "/api/exportar-csv":
            audit_id = int(query.get("id", [0])[0])
            token_param = query.get("token", [None])[0]
            tok_payload = None
            if token_param:
                tok_payload = validar_token_compartilhamento(token_param)
                if not tok_payload:
                    self._send_json({"erro": "Token inválido para exportação CSV."}, status=403)
                    return
            elif (not is_admin) and viewer_lock is not None:
                tok_payload = viewer_lock
            elif (not is_admin) and REQUIRE_ADMIN_LOGIN:
                self._send_json({"erro": "Autenticação administrativa necessária."}, status=401)
                return

            if tok_payload and audit_id > 0:
                conn = db.get_connection()
                try:
                    row = conn.execute("SELECT contrato_id FROM auditorias WHERE id = ?", (audit_id,)).fetchone()
                    if not row or row["contrato_id"] != tok_payload["cidade"]:
                        self._send_json({"erro": "CSV não pertence à cidade deste link."}, status=403)
                        return
                finally:
                    conn.close()

            csv_str, filename = engine.gerar_csv_omissoes_string(audit_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(csv_str.encode("utf-8-sig"))
            return

        # Demais APIs internas e Inlog: exigem sessão administrativa autenticada
        if path.startswith("/api/") and (not is_admin) and (viewer_lock is not None or REQUIRE_ADMIN_LOGIN):
            self._send_json({"sucesso": False, "erro": "Acesso restrito ao Painel Central autenticado."}, status=403)
            return

        # 3b. Listar Snapshots Gerados (Painel Central)
        if path == "/api/compartilhar/listar":
            cid_filtro = query.get("cidade", [None])[0]
            self._send_json({"sucesso": True, "snapshots": snapshots.listar_snapshots(contrato_id=cid_filtro)})
            return

        # 4. Exportação GeoJSON da Malha Viária Local
        if path == "/api/exportar-geojson":
            if (not is_admin) and (viewer_lock is not None or REQUIRE_ADMIN_LOGIN):
                self._send_json({"sucesso": False, "erro": "Acesso restrito ao Painel Central."}, status=403)
                return
            cidade = query.get("cidade", ["cabo"])[0].lower().strip()
            conn = db.get_connection()
            try:
                rows = conn.execute(
                    "SELECT id, fid, nome_rua, setor, tipo, is_manual, is_priority, extensao_m, geom_json FROM segmentos_viarios WHERE contrato_id = ?",
                    (cidade,),
                ).fetchall()
                features = [
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": json.loads(r["geom_json"])},
                        "properties": {
                            "id": r["id"],
                            "fid": r["fid"],
                            "street_name": r["nome_rua"],
                            "sector": r["setor"],
                            "tipo": r["tipo"],
                            "is_manual": bool(r["is_manual"]),
                            "is_priority": bool(r["is_priority"]),
                            "len_m": r["extensao_m"],
                        },
                    }
                    for r in rows
                ]
                geojson = {"type": "FeatureCollection", "features": features}
                self.send_response(200)
                self.send_header("Content-Type", "application/geo+json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="malha_{cidade}.geojson"')
                self.end_headers()
                self.wfile.write(json.dumps(geojson, ensure_ascii=False).encode("utf-8"))
            finally:
                conn.close()
            return

        # 5. Status da Conexão Inlog
        if path == "/api/status":
            saude = inlog.status_conexao()
            self._send_json({
                "inlog_configurado": inlog.is_configured(),
                "base_url": inlog.base_url,
                **saude,
            })
            return

        # 6. Visão Geral Multi-Cidades (Todas as cidades)
        if path == "/api/inlog/visao-geral":
            if (not is_admin) and (viewer_lock is not None or REQUIRE_ADMIN_LOGIN):
                self._send_json({"sucesso": False, "erro": "Acesso restrito ao Painel Central."}, status=403)
                return
            data_iso = query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            veiculos = inlog.get_veiculos_tempo_real()
            setores = inlog.get_cadastro_setores()

            conn = db.get_connection()
            try:
                contratos = conn.execute("SELECT id, nome FROM contratos ORDER BY nome").fetchall()
                vias_map = dict(conn.execute("SELECT contrato_id, COUNT(*) FROM segmentos_viarios GROUP BY contrato_id").fetchall())
                aud_map = dict(
                    conn.execute(
                        "SELECT contrato_id, pct_cobertura FROM auditorias WHERE data_iso = ? AND turno = 'DIURNO'",
                        (data_iso,),
                    ).fetchall()
                )
            finally:
                conn.close()

            setores_count = {}
            for s in setores:
                cid = s.get("contrato_id")
                if cid:
                    setores_count[cid] = setores_count.get(cid, 0) + 1

            frota_stats = {}
            total_ligados = 0
            total_conectados = 0
            for v in veiculos:
                if v.get("statusIgnicaoVeiculo") == "L":
                    total_ligados += 1
                if v.get("statusConexaoAVL") == "T":
                    total_conectados += 1
                cids_v = set()
                for g in v.get("gruposControle") or []:
                    cid = identificar_contrato(g.get("descricao"))
                    if cid:
                        cids_v.add(cid)
                for cid in cids_v:
                    st = frota_stats.setdefault(cid, {"total": 0, "ligados": 0, "conectados": 0})
                    st["total"] += 1
                    if v.get("statusIgnicaoVeiculo") == "L":
                        st["ligados"] += 1
                    if v.get("statusConexaoAVL") == "T":
                        st["conectados"] += 1

            cidades_out = []
            for c in contratos:
                cid = c["id"]
                fs = frota_stats.get(cid, {"total": 0, "ligados": 0, "conectados": 0})
                cidades_out.append({
                    "id": cid,
                    "nome": c["nome"],
                    "total_veiculos": fs["total"],
                    "veiculos_ligados": fs["ligados"],
                    "veiculos_conectados": fs["conectados"],
                    "setores_inlog": setores_count.get(cid, 0),
                    "vias_malha": vias_map.get(cid, 0),
                    "cobertura_pct": aud_map.get(cid),
                })

            self._send_json({
                "sucesso": True,
                "data_iso": data_iso,
                "total_veiculos_frota": len(veiculos),
                "total_ligados": total_ligados,
                "total_conectados": total_conectados,
                "cidades": cidades_out,
            })
            return

        # 7. Veículos em Tempo Real Enriquecidos (Veiculos/TempoReal + Veiculos + Motoristas)
        if path == "/api/inlog/veiculos-tempo-real":
            cidade = query.get("cidade", ["cabo"])[0]
            operacao = query.get("operacao", ["COLETA"])[0]
            force = query.get("force", ["false"])[0].lower() == "true"

            veic_rt = inlog.get_veiculos_tempo_real(contrato_id=cidade, operacao=operacao, force_refresh=force)
            cad_map = {v["placa"]: v for v in inlog.get_cadastro_veiculos(force_refresh=force)}
            mot_map = {m["matricula"]: m["nome"] for m in inlog.get_cadastro_motoristas(force_refresh=force)}

            enriquecidos = []
            for v in veic_rt:
                pl = formatar_placa_hifen(v.get("placaVeiculo") or v.get("identificadorVeiculo"))
                mat = str(v.get("matriculaMotorista") or "").strip()
                item = dict(v)
                item["cadastro"] = cad_map.get(pl)
                item["motoristaNome"] = mot_map.get(mat)
                enriquecidos.append(item)

            self._send_json({"sucesso": True, "total": len(enriquecidos), "veiculos": enriquecidos})
            return

        # 8. Rastro GPS Real (Historico)
        if path == "/api/inlog/rastro-gps":
            cidade = query.get("cidade", ["cabo"])[0]
            data_iso = query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            operacao = query.get("operacao", ["COLETA"])[0]
            placa = query.get("placa", ["all"])[0]

            por_placa = {}
            if placa and placa.lower() != "all":
                pts = inlog.get_historico_gps_janela_operacional(placa, data_iso, contrato_id=cidade)
                por_placa[formatar_placa_hifen(placa)] = [
                    {
                        "lat": p["latitude"],
                        "lon": p["longitude"],
                        "dataHora": p["dataHora"],
                        "velocidade": p.get("velocidade", 0),
                    }
                    for p in pts
                ]
            else:
                _, _, gps_map = engine._coletar_frota_e_gps_cidade(cidade, data_iso, operacao=operacao, force_refresh=False)
                for pl, pts in gps_map.items():
                    if pts:
                        por_placa[pl] = [
                            {
                                "lat": p["latitude"],
                                "lon": p["longitude"],
                                "dataHora": p["dataHora"],
                                "velocidade": p.get("velocidade", 0),
                            }
                            for p in pts
                        ]

            total_pts = sum(len(v) for v in por_placa.values())
            self._send_json({"sucesso": True, "data_iso": data_iso, "total_pontos": total_pts, "por_placa": por_placa})
            return

        # 9. Setores (Cadastro + Tempo Real)
        if path == "/api/inlog/setores":
            cidade = query.get("cidade", ["cabo"])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            cad = inlog.get_cadastro_setores(contrato_id=cidade, force_refresh=force)
            rt = inlog.get_setores_tempo_real(contrato_id=cidade, force_refresh=force)
            self._send_json({"sucesso": True, "cadastro": cad, "tempo_real": rt})
            return

        # 10. Relatórios Diários (RD + Macros + Pesagem)
        if path == "/api/inlog/relatorio-diario":
            cidade = query.get("cidade", ["cabo"])[0]
            data_iso = query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            regs = inlog.get_relatorio_diario(data_iso, contrato_id=cidade, force_refresh=force)
            peso_total = sum(float(r.get("pesoTotalKg") or 0.0) for r in regs)
            self._send_json({"sucesso": True, "total": len(regs), "peso_total_kg": round(peso_total, 2), "registros": regs})
            return

        # 11. Alarmes e Falhas
        if path == "/api/inlog/alarmes-falhas":
            cidade = query.get("cidade", ["cabo"])[0]
            data_iso = query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            alarmes = inlog.get_alarmes(data_iso, contrato_id=cidade, force_refresh=force)
            falhas = inlog.get_falhas(data_iso, contrato_id=cidade, force_refresh=force)
            self._send_json({"sucesso": True, "alarmes": alarmes, "falhas": falhas})
            return

        # 12. Telemetria (Faixa Verde)
        if path == "/api/inlog/telemetria":
            cidade = query.get("cidade", ["cabo"])[0]
            data_iso = query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            placa = query.get("placa", [None])[0]
            force = query.get("force", ["false"])[0].lower() == "true"
            if placa and placa.lower() == "all":
                placa = None
            rows = inlog.get_telemetria(data_iso, placa=placa, contrato_id=cidade, force_refresh=force)

            agregado = {}
            for r in rows:
                pl = r["placa"]
                ag = agregado.setdefault(pl, {
                    "placa": pl,
                    "janelas": 0,
                    "tempo_movimento_s": 0.0,
                    "tempo_faixa_verde_s": 0.0,
                    "tempo_disponivel_fv_s": 0.0,
                    "qtde_freio_pe": 0,
                    "qtde_freio_estac": 0,
                })
                ag["janelas"] += 1
                ag["tempo_movimento_s"] += float(r.get("tempoMovimento") or 0.0)
                ag["tempo_faixa_verde_s"] += float(r.get("tempoEmFaixaVerde") or 0.0)
                ag["tempo_disponivel_fv_s"] += float(r.get("tempoDisponivelFaixaVerde") or 0.0)
                ag["qtde_freio_pe"] += int(r.get("qtdeFreioPe") or 0)
                ag["qtde_freio_estac"] += int(r.get("qtdeFreioEstacionamento") or 0)

            resumo = []
            for pl, ag in sorted(agregado.items()):
                disp = ag["tempo_disponivel_fv_s"]
                ag["pct_faixa_verde"] = round((ag["tempo_faixa_verde_s"] / disp * 100.0), 1) if disp > 0 else 0.0
                resumo.append(ag)

            self._send_json({"sucesso": True, "total_registros": len(rows), "resumo_veiculos": resumo})
            return

        # 13. Motoristas
        if path == "/api/inlog/motoristas":
            force = query.get("force", ["false"])[0].lower() == "true"
            mots = inlog.get_cadastro_motoristas(force_refresh=force)
            self._send_json({"sucesso": True, "total": len(mots), "motoristas": mots})
            return

        # 14. Diagnóstico & Logs HTTP Inlog
        if path == "/api/inlog/logs":
            if (not is_admin) and (viewer_lock is not None or REQUIRE_ADMIN_LOGIN):
                self._send_json({"sucesso": False, "erro": "Acesso restrito ao Painel Central."}, status=403)
                return
            conn = db.get_connection()
            try:
                logs = [dict(r) for r in conn.execute("SELECT * FROM inlog_sync_logs ORDER BY id DESC LIMIT 150").fetchall()]
                tabelas = [
                    "segmentos_viarios", "inlog_veiculos", "inlog_veiculos_tempo_real",
                    "inlog_setores", "inlog_setores_tempo_real", "inlog_motoristas",
                    "inlog_historico_gps", "inlog_relatorios_diarios", "inlog_alarmes",
                    "inlog_falhas", "inlog_telemetria",
                ]
                stats = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabelas}
            finally:
                conn.close()
            self._send_json({"sucesso": True, "logs": logs, "estatisticas_banco": stats})
            return

        # 15. Malha Viária Oficial por Viewport (.gpkg)
        if path == "/api/malha-viaria":
            cidade = query.get("cidade", ["cabo"])[0].lower().strip()
            bbox_raw = query.get("bbox", [""])[0]
            try:
                partes = [float(x) for x in bbox_raw.split(",")]
                if len(partes) != 4:
                    raise ValueError
                bbox = (partes[0], partes[1], partes[2], partes[3])
            except ValueError:
                self._send_json({"type": "FeatureCollection", "features": [], "erro": "bbox inválido"})
                return
            self._send_json(malha_rotas.malha_viewport(cidade, bbox))
            return

        # 16. Rota Planejada (.gpkg) por Veículo (via Inlog) ou por Setor
        if path == "/api/rota-planejada":
            cidade = query.get("cidade", ["cabo"])[0].lower().strip()
            data_iso = query.get("data", [datetime.now().strftime("%Y-%m-%d")])[0]
            placa = formatar_placa_hifen(query.get("placa", [""])[0])
            setor_param = query.get("setor", [""])[0].strip()

            rotas_alvo = []
            if setor_param:
                r = malha_rotas.resolver_rota_setor(cidade, setor_param, data_iso=data_iso)
                if r:
                    rotas_alvo.append(r)

            if placa:
                hoje_str = datetime.now().strftime("%Y-%m-%d")
                setores_veic = []
                for item in inlog.get_relatorio_diario(data_iso, contrato_id=cidade, force_refresh=False):
                    if formatar_placa_hifen(item.get("placa") or item.get("veiculo")) == placa and item.get("setor"):
                        setores_veic.append((item["setor"], ""))
                if data_iso == hoje_str:
                    for s in inlog.get_setores_tempo_real(contrato_id=cidade, force_refresh=False):
                        if formatar_placa_hifen(s.get("placaVeiculo") or s.get("identificadorVeiculo")) == placa and s.get("setor"):
                            setores_veic.append((s["setor"], s.get("turno") or ""))
                vistos = set()
                for s_nome, turno in setores_veic:
                    r = malha_rotas.resolver_rota_setor(cidade, s_nome, data_iso=data_iso, turno=turno)
                    if r and r["path"] not in vistos:
                        vistos.add(r["path"])
                        rotas_alvo.append(r)

            features = []
            km_coleta = 0.0
            for r in rotas_alvo:
                for a in malha_rotas.carregar_rota(r):
                    if a["is_collection"]:
                        km_coleta += a["len_m"] / 1000.0
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": a["coords"]},
                        "properties": {
                            "rota": r["apelido"],
                            "turno": r["turno"],
                            "seq": a["seq"],
                            "is_collection": a["is_collection"],
                            "nome": a["nome"],
                            "len_m": a["len_m"],
                            "edge_key": a["edge_key"],
                        },
                    })
            self._send_json({
                "sucesso": True,
                "rotas": [r["apelido"] for r in rotas_alvo],
                "km_coleta": round(km_coleta, 2),
                "geojson": {"type": "FeatureCollection", "features": features},
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        dados = json.loads(body) if body else {}
        is_admin, viewer_lock, _ = self._obter_sessao()

        # 0. Autenticação Administrativa (Desbloqueio do Painel Central)
        if path == "/api/auth/login":
            senha = str(dados.get("password") or "")
            if hmac.compare_digest(senha, ADMIN_PASSWORD):
                admin_cookie = gerar_cookie_admin()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Set-Cookie", f"lc_admin_auth={admin_cookie}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Set-Cookie", "lc_viewer_lock=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(json.dumps({"sucesso": True, "redirect": "/"}).encode("utf-8"))
            else:
                self._send_json({"sucesso": False, "erro": "Chave de acesso inválida."}, status=401)
            return

        if (not is_admin) and (viewer_lock is not None or REQUIRE_ADMIN_LOGIN):
            self._send_json({"sucesso": False, "erro": "Ação bloqueada: sessão restrita ao modo visualização."}, status=403)
            return

        # 0b. Gerar Link Snapshot Congelado (ou atualizar se o dia ainda estiver em andamento)
        if path == "/api/compartilhar/gerar":
            try:
                forcar_recriar = bool(dados.get("forcar", False))
                snap = snapshots.gerar_ou_atualizar_snapshot(engine, dados, forcar_recriar=forcar_recriar)
                self._send_json({
                    "sucesso": True,
                    "modo": "snapshot",
                    "id": snap["id"],
                    "path": snap["path"],
                    "reutilizado": snap["reutilizado"],
                    "recriado": snap["recriado"],
                    "janela_fechada": snap["janela_fechada"],
                    "criado_em": snap["criado_em"],
                    "atualizado_em": snap["atualizado_em"],
                    "tamanho_kb": round((snap["tamanho_gzip"] or 0) / 1024.0, 1),
                })
            except Exception as e:
                self._send_json({"sucesso": False, "erro": str(e)}, status=400)
            return

        # 0c. Revogar Link Snapshot
        if path == "/api/compartilhar/revogar":
            try:
                snap_id = str(dados.get("id") or "").strip()
                ok = snapshots.revogar_snapshot(snap_id)
                self._send_json({"sucesso": ok})
            except Exception as e:
                self._send_json({"sucesso": False, "erro": str(e)}, status=400)
            return

        # 1. Salvar ou Criar Segmento no Projeto
        if path == "/api/segmento/salvar":
            try:
                res = db.salvar_ou_atualizar_segmento(dados)
                contrato_id = str(dados.get("contrato_id", "")).lower().strip()
                if contrato_id:
                    conn = db.get_connection()
                    try:
                        with conn:
                            conn.execute("DELETE FROM auditorias WHERE contrato_id = ?", (contrato_id,))
                    finally:
                        conn.close()
                self._send_json({"sucesso": True, "segmento": res})
            except Exception as e:
                self._send_json({"sucesso": False, "erro": str(e)}, status=500)
            return

        # 2. Excluir Segmento do Projeto
        if path == "/api/segmento/excluir":
            try:
                seg_id = dados.get("id")
                conn = db.get_connection()
                try:
                    row = conn.execute("SELECT contrato_id FROM segmentos_viarios WHERE id = ?", (seg_id,)).fetchone()
                    contrato_id = row["contrato_id"] if row else None
                finally:
                    conn.close()

                db.excluir_segmento(seg_id)

                if contrato_id:
                    conn = db.get_connection()
                    try:
                        with conn:
                            conn.execute("DELETE FROM auditorias WHERE contrato_id = ?", (contrato_id,))
                    finally:
                        conn.close()
                self._send_json({"sucesso": True})
            except Exception as e:
                self._send_json({"sucesso": False, "erro": str(e)}, status=500)
            return

        # 3. Importar Malha GeoJSON para qualquer cidade
        if path == "/api/malha/importar":
            try:
                contrato_id = dados.get("contrato_id", "")
                geojson_obj = dados.get("geojson") or {}
                substituir = bool(dados.get("substituir", False))
                qtd = db.importar_malha_geojson(contrato_id, geojson_obj, substituir=substituir)
                self._send_json({"sucesso": True, "importados": qtd})
            except Exception as e:
                self._send_json({"sucesso": False, "erro": str(e)}, status=400)
            return

        # 4. Reconstruir Projeto a partir da Malha Viária (.gpkg) e Rotas Planejadas
        if path == "/api/malha/reconstruir":
            try:
                contrato_id = dados.get("contrato_id", "")
                res = malha_rotas.construir_projeto_cidade(contrato_id, forcar=True)
                self._send_json(res)
            except Exception as e:
                self._send_json({"sucesso": False, "erro": str(e)}, status=500)
            return

        self.send_response(404)
        self.end_headers()


def _inicializar_background():
    """Constrói malhas pendentes e pré-aquece suavemente auditorias de ontem sem travar a CPU."""
    time.sleep(3)
    try:
        malha_rotas.preconstruir_todas_cidades()
    except Exception as e:
        print(f"[STARTUP] Aviso ao pre-construir malhas: {e}")

    if inlog.is_configured():
        data_ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = db.get_connection()
        try:
            cids = [r["id"] for r in conn.execute("SELECT id FROM contratos ORDER BY nome").fetchall()]
        finally:
            conn.close()
        for cid in cids:
            try:
                engine.processar_ou_obter_auditoria(cid, data_ontem, forcar_reprocessamento=False, operacao="COLETA")
                time.sleep(0.3)
            except Exception as e:
                print(f"[STARTUP] Aviso no pré-aquecimento ({cid}/{data_ontem}): {e}")


def run(port: int = 8080):
    host = os.getenv("HOST", "0.0.0.0")
    server_address = (host, port)
    threading.Thread(target=_inicializar_background, daemon=True).start()
    httpd = ThreadingHTTPServer(server_address, LocarRequestHandler)
    print("=" * 72)
    print(" LOCAR SANEAMENTO AMBIENTAL - SISTEMA LC v1 (INLOG 100% REAL)")
    print(f" Servidor multi-thread ativo em: http://{host}:{port}")
    print(f" Banco ativo: {'PostgreSQL (DATABASE_URL)' if db.IS_POSTGRES else f'SQLite ({db.DB_PATH})'}")
    print("=" * 72)
    httpd.serve_forever()


if __name__ == "__main__":
    porta = int(os.getenv("PORT", 8080))
    run(porta)
