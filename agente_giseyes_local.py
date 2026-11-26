"""
Agente Local GIS Eyes (clientetvt4.exe) — 100% Standalone (somente Python padrão).

Permite que o navegador do usuário no Windows (acessando o Sistema LC deployado
no Railway/nuvem) envie o Shapefile do Setor (.zip) para:
    http://127.0.0.1:18765/atualizar-setor

O agente abre o clientetvt4.exe no Windows, faz login (LOCAR / GABRIELLIMA / inlog2026),
abre Cadastros -> GIS -> Importador, carrega o .shp do setor, seleciona os 7 combos
(DOMICILIAR F2 para Cuiabá, DOMICILIAR F1 para as demais cidades), clica em Processar,
confirma os diálogos de finalização e encerra o aplicativo.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as _wt
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

PORT = int(os.getenv("GISEYES_AGENT_PORT", "18765"))
GISEYES_DEFAULT_EMPRESA = os.getenv("GISEYES_EMPRESA", "LOCAR").strip()
GISEYES_DEFAULT_USER = os.getenv("GISEYES_USER", "GABRIELLIMA").strip()
GISEYES_DEFAULT_PASS = os.getenv("GISEYES_PASS", "inlog2026").strip()


def localizar_clientetvt4_exe() -> Optional[str]:
    """Localiza o executável clientetvt4.exe instalado nesta máquina Windows."""
    home = os.path.expanduser("~")
    candidatos = [
        os.getenv("GISEYES_EXE_PATH", "").strip(),
        os.path.join(home, "Downloads", "GisEyes", "GisEyes", "clientetvt4.exe"),
        os.path.join(home, "Downloads", "GisEyes", "clientetvt4.exe"),
        os.path.join(home, "Downloads", "FS - DIE", "FS - DIE", "GisEyes", "clientetvt4.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "GisEyes", "clientetvt4.exe"),
        r"C:\GisEyes\clientetvt4.exe",
        r"C:\Program Files (x86)\GisEyes\clientetvt4.exe",
        r"C:\Program Files\GisEyes\clientetvt4.exe",
    ]
    for p in candidatos:
        if p and os.path.isfile(p):
            return p
    for pattern in (
        os.path.join(home, "Downloads", "**", "clientetvt4.exe"),
        os.path.join(home, "Desktop", "**", "clientetvt4.exe"),
    ):
        for p in glob.iglob(pattern, recursive=True):
            if os.path.isfile(p):
                return p
    return None


def ler_colunas_dbf(shp_path: str) -> List[str]:
    """Lê os nomes das colunas diretamente do arquivo .dbf (formato dBASE III) sem dependências externas."""
    dbf_path = os.path.splitext(shp_path)[0] + ".dbf"
    if not os.path.isfile(dbf_path):
        raise FileNotFoundError(f"Arquivo .dbf correspondente não encontrado: {dbf_path}")
    cols: List[str] = []
    with open(dbf_path, "rb") as f:
        f.seek(32)
        while True:
            rec = f.read(32)
            if not rec or len(rec) < 32 or rec[0] == 0x0D:
                break
            name = rec[:11].split(b"\x00", 1)[0].decode("latin1", "ignore").strip()
            if name:
                cols.append(name)
    return cols


def atualizar_setor_clientetvt4(
    zip_setor_bytes: bytes,
    contrato_id: str,
    empresa: str = GISEYES_DEFAULT_EMPRESA,
    usuario: str = GISEYES_DEFAULT_USER,
    senha: str = GISEYES_DEFAULT_PASS,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    def _log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    if os.name != "nt" or sys.platform != "win32":
        raise RuntimeError("Este agente local só pode ser executado no Windows.")

    exe_path = localizar_clientetvt4_exe()
    if not exe_path:
        raise FileNotFoundError(
            "Executável 'clientetvt4.exe' não encontrado no PC. "
            "Extraia a pasta GisEyes na sua pasta Downloads."
        )

    cid = str(contrato_id or "").lower().strip()
    operacao_nome = "DOMICILIAR F2" if cid == "cuiaba" else "DOMICILIAR F1"
    operacao_idx = 14 if cid == "cuiaba" else 0

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    WM_SETTEXT = 0x000C
    WM_GETTEXT = 0x000D
    WM_GETTEXTLENGTH = 0x000E
    WM_CHAR = 0x0102
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_MOUSEMOVE = 0x0200
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_COMMAND = 0x0111
    VK_UP = 0x26
    VK_DOWN = 0x28

    def _get_text(hwnd: int) -> str:
        ln = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
        b = ctypes.create_unicode_buffer(max(256, ln + 2))
        user32.SendMessageW(hwnd, WM_GETTEXT, len(b), ctypes.byref(b))
        return b.value

    def _get_cls(hwnd: int) -> str:
        b = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, b, 256)
        return b.value

    def _get_rect(hwnd: int) -> Tuple[int, int, int, int]:
        rc = _wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return (rc.left, rc.top, rc.right, rc.bottom)

    def _click(hwnd: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        rc = _get_rect(hwnd)
        cx = max(4, (rc[2] - rc[0]) // 2) if x is None else x
        cy = max(4, (rc[3] - rc[1]) // 2) if y is None else y
        lparam = (cy << 16) | (cx & 0xFFFF)
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)

    def _type_edit(hwnd: int, text: str) -> None:
        user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(""))
        for _ in range(30):
            user32.SendMessageW(hwnd, WM_CHAR, 0x08, 0)
        for ch in text:
            user32.SendMessageW(hwnd, WM_CHAR, ord(ch), 0)

    def _find_popup_datalist(pid: int) -> Tuple[Optional[int], Optional[Tuple[int, int, int, int]]]:
        found: List[Tuple[int, Tuple[int, int, int, int]]] = []

        def _cb(hwnd, _lp):
            p = _wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsPopupDataList":
                found.append((hwnd, _get_rect(hwnd)))
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return found[0] if found else (None, None)

    def _select_combo_index(pid: int, combo_hwnd: int, target_index: int) -> None:
        rc = _get_rect(combo_hwnd)
        w, h = rc[2] - rc[0], rc[3] - rc[1]
        _click(combo_hwnd, w - 10, h // 2)
        time.sleep(0.30)
        pop_hwnd, _ = _find_popup_datalist(pid)
        if pop_hwnd:
            _click(pop_hwnd, 40, 10)
            time.sleep(0.20)
        for _ in range(35):
            user32.SendMessageW(combo_hwnd, WM_KEYDOWN, VK_UP, 0)
            user32.SendMessageW(combo_hwnd, WM_KEYUP, VK_UP, 0)
        for _ in range(target_index):
            user32.SendMessageW(combo_hwnd, WM_KEYDOWN, VK_DOWN, 0)
            user32.SendMessageW(combo_hwnd, WM_KEYUP, VK_DOWN, 0)
        time.sleep(0.15)

    with tempfile.TemporaryDirectory(prefix="giseyes_imp_") as tmpdir:
        with zipfile.ZipFile(io.BytesIO(zip_setor_bytes)) as zf:
            zf.extractall(tmpdir)
        shp_files = list(glob.iglob(os.path.join(tmpdir, "**", "*.shp"), recursive=True))
        if not shp_files:
            raise RuntimeError("Nenhum arquivo .shp encontrado dentro do ZIP do setor.")
        shp_path = shp_files[0]
        dbf_cols = ler_colunas_dbf(shp_path)

        def _col_rev_idx(col_name: str) -> int:
            if col_name not in dbf_cols:
                raise RuntimeError(f"Coluna '{col_name}' ausente no Shapefile de setor ({dbf_cols}).")
            return len(dbf_cols) - 1 - dbf_cols.index(col_name)

        _log(f"🖥️ Abrindo GIS Eyes ({os.path.basename(exe_path)}) e conectando como {empresa}/{usuario}...")
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
            pid = proc.pid

            # 1. Aguardar tela de Login (TfrmLoginSistema)
            login_hwnd = None
            main_hwnd = None
            for _ in range(40):
                if proc.poll() is not None:
                    raise RuntimeError("clientetvt4.exe encerrou inesperadamente durante a abertura.")
                time.sleep(1.0)

                def _find_login(hwnd, _lp):
                    nonlocal login_hwnd, main_hwnd
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value != pid:
                        return True
                    cls = _get_cls(hwnd)
                    if cls == "TfrmPricipalClienteTVT4" and user32.IsWindowVisible(hwnd):
                        main_hwnd = hwnd
                    elif cls == "TfrmLoginSistema" and user32.IsWindowVisible(hwnd):
                        login_hwnd = hwnd
                    return True

                user32.EnumWindows(WNDENUMPROC(_find_login), 0)
                if login_hwnd and main_hwnd:
                    break

            if not login_hwnd or not main_hwnd:
                raise RuntimeError("Tempo esgotado aguardando a tela de login (Entrada de Usuários) do GIS Eyes.")

            l_children: List[Tuple[int, str, str, Tuple[int, int, int, int]]] = []

            def _enum_login(ch, _lp):
                l_children.append((ch, _get_cls(ch), _get_text(ch), _get_rect(ch)))
                return True

            user32.EnumChildWindows(login_hwnd, WNDENUMPROC(_enum_login), 0)
            edits = sorted([c for c in l_children if c[1] == "TbsSkinEdit"], key=lambda x: x[3][1])
            pwds = [c for c in l_children if c[1] == "TbsSkinPasswordEdit"]
            ok_btns = [c for c in l_children if c[1] == "TbsSkinButton" and "OK" in c[2].upper()]
            if len(edits) < 2 or not pwds or not ok_btns:
                raise RuntimeError("Controles da tela de login do GIS Eyes não reconhecidos.")

            _type_edit(edits[0][0], empresa)
            _type_edit(edits[1][0], usuario)
            _type_edit(pwds[0][0], senha)
            _click(ok_btns[0][0])

            # 2. Aguardar conclusão do login e sumiço de TfrmAguarde
            logged_ok = False
            for _ in range(30):
                time.sleep(1.0)
                st_texts: List[str] = []

                def _enum_st(ch, _lp):
                    if _get_cls(ch) == "TbsSkinStatusPanel":
                        st_texts.append(_get_text(ch))
                    return True

                user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_enum_st), 0)
                aguarde_or_login = False
                err_popups: List[Tuple[int, str]] = []

                def _chk_wait(hwnd, _lp):
                    nonlocal aguarde_or_login
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value != pid or not user32.IsWindowVisible(hwnd):
                        return True
                    cls = _get_cls(hwnd)
                    if cls in ("TfrmAguarde", "TfrmLoginSistema"):
                        aguarde_or_login = True
                    elif cls == "TbsMessageForm":
                        err_popups.append((hwnd, _get_text(hwnd)))
                    return True

                user32.EnumWindows(WNDENUMPROC(_chk_wait), 0)
                if err_popups:
                    raise RuntimeError(f"GIS Eyes exibiu alerta no login: {err_popups[0][1]}")
                if not aguarde_or_login and any("LOCAR" in t.upper() for t in st_texts):
                    logged_ok = True
                    break

            if not logged_ok:
                raise RuntimeError("Login no GIS Eyes não completou dentro do tempo limite.")

            _log(f"🔓 Login no GIS Eyes concluído ({empresa}/{usuario}). Abrindo Cadastros -> GIS -> Importador...")
            time.sleep(0.8)

            # 3. Abrir Cadastros -> GIS -> Importador
            menubar_hwnd = None

            def _find_menu(ch, _lp):
                nonlocal menubar_hwnd
                if _get_cls(ch) == "TbsSkinMainMenuBar":
                    menubar_hwnd = ch
                return True

            user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_find_menu), 0)
            if not menubar_hwnd:
                raise RuntimeError("Barra de menu principal do GIS Eyes não localizada.")

            _click(menubar_hwnd, 70, 10)
            time.sleep(0.7)
            pop1 = None
            pop1_rc = None

            def _find_pop1(hwnd, _lp):
                nonlocal pop1, pop1_rc
                p = _wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsSkinPopupWindow":
                    pop1 = hwnd
                    pop1_rc = _get_rect(hwnd)
                return True

            user32.EnumWindows(WNDENUMPROC(_find_pop1), 0)
            if not pop1 or not pop1_rc:
                raise RuntimeError("Menu 'Cadastros' do GIS Eyes não abriu.")

            _click(pop1, (pop1_rc[2] - pop1_rc[0]) // 2, int((pop1_rc[3] - pop1_rc[1]) * 5.5 / 8))
            time.sleep(0.7)

            pop2 = None
            pop2_rc = None

            def _find_pop2(hwnd, _lp):
                nonlocal pop2, pop2_rc
                p = _wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsSkinPopupWindow" and hwnd != pop1:
                    pop2 = hwnd
                    pop2_rc = _get_rect(hwnd)
                return True

            user32.EnumWindows(WNDENUMPROC(_find_pop2), 0)
            if not pop2 or not pop2_rc:
                raise RuntimeError("Submenu 'GIS' do GIS Eyes não abriu.")

            _click(pop2, (pop2_rc[2] - pop2_rc[0]) // 2, 14)

            btn_abrir = None
            btn_processar = None
            for _ in range(15):
                time.sleep(0.5)

                def _find_imp(ch, _lp):
                    nonlocal btn_abrir, btn_processar
                    if user32.IsWindowVisible(ch) and _get_cls(ch) == "TbsSkinButton":
                        t = _get_text(ch)
                        if t == "Abrir":
                            btn_abrir = ch
                        elif t == "Processar":
                            btn_processar = ch
                    return True

                user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_find_imp), 0)
                if btn_abrir and btn_processar:
                    break
            if not btn_abrir or not btn_processar:
                raise RuntimeError("Tela do Importador GIS não abriu corretamente.")

            # 4. Clicar em Abrir e enviar o .shp no diálogo #32770
            _click(btn_abrir)
            dlg_hwnd = None
            for _ in range(15):
                time.sleep(0.5)

                def _find_dlg(hwnd, _lp):
                    nonlocal dlg_hwnd
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "#32770" and _get_text(hwnd) == "Abrir":
                        dlg_hwnd = hwnd
                    return True

                user32.EnumWindows(WNDENUMPROC(_find_dlg), 0)
                if dlg_hwnd:
                    break
            if not dlg_hwnd:
                raise RuntimeError("Diálogo 'Abrir' de arquivo do Windows não apareceu.")

            edit_1148 = None
            btn_1 = None

            def _enum_dlg(ch, _lp):
                nonlocal edit_1148, btn_1
                cid_ctrl = user32.GetDlgCtrlID(ch)
                ccls = _get_cls(ch)
                if ccls == "Edit" and cid_ctrl == 1148:
                    edit_1148 = ch
                elif ccls == "Button" and cid_ctrl == 1:
                    btn_1 = ch
                return True

            user32.EnumChildWindows(dlg_hwnd, WNDENUMPROC(_enum_dlg), 0)
            if not edit_1148 or not btn_1:
                raise RuntimeError("Campo de caminho do arquivo no diálogo Abrir não encontrado.")

            user32.SendMessageW(edit_1148, WM_SETTEXT, 0, ctypes.c_wchar_p(shp_path))
            time.sleep(0.25)
            user32.SendMessageW(dlg_hwnd, WM_COMMAND, 1, btn_1)

            loaded_shp = False
            for _ in range(20):
                time.sleep(0.5)
                if not user32.IsWindowVisible(dlg_hwnd):
                    titles: List[str] = []

                    def _chk_imp(ch, _lp):
                        if _get_cls(ch) == "TfrmImportadorShape":
                            titles.append(_get_text(ch))
                        return True

                    user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_chk_imp), 0)
                    if any(".shp" in t.lower() for t in titles):
                        loaded_shp = True
                        break
            if not loaded_shp:
                time.sleep(1.5)

            # 5. Mapear os 8 TbsSkinDBLookupComboBox da aba Setores
            combos: List[Tuple[int, Tuple[int, int, int, int]]] = []

            def _enum_combos(ch, _lp):
                if user32.IsWindowVisible(ch) and _get_cls(ch) == "TbsSkinDBLookupComboBox":
                    combos.append((ch, _get_rect(ch)))
                return True

            user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_enum_combos), 0)
            combos.sort(key=lambda x: x[1][1])
            if len(combos) < 8:
                raise RuntimeError(f"Esperados 8 combos na aba Setores, encontrados {len(combos)}.")

            _log(f"⚙️ Mapeando colunas do setor e configurando Operação '{operacao_nome}' no GIS Eyes...")
            _select_combo_index(pid, combos[0][0], _col_rev_idx("SETOR"))
            _select_combo_index(pid, combos[1][0], 0)
            _select_combo_index(pid, combos[2][0], _col_rev_idx("APELIDO"))
            _select_combo_index(pid, combos[3][0], _col_rev_idx("IDENTIF"))
            _select_combo_index(pid, combos[4][0], _col_rev_idx("FREQUENCIA"))
            _select_combo_index(pid, combos[5][0], _col_rev_idx("MACRO"))
            _select_combo_index(pid, combos[7][0], operacao_idx)

            # 6. Clicar em Processar e confirmar os diálogos TbsMessageForm
            _log("🚀 Clicando em Processar no GIS Eyes para gravar o setor na base Inlog...")
            _click(btn_processar)

            mensagens_confirmadas: List[str] = []
            deadline = time.time() + 60.0
            while time.time() < deadline:
                time.sleep(0.8)
                msgs_now: List[Tuple[int, str, Optional[int]]] = []

                def _find_msgs(hwnd, _lp):
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsMessageForm":
                        ok_btn = None

                        def _mb(ch, _lp2):
                            nonlocal ok_btn
                            if _get_cls(ch) == "TbsSkinButton" and _get_text(ch).upper() in ("OK", "SIM"):
                                ok_btn = ch
                            return True

                        user32.EnumChildWindows(hwnd, WNDENUMPROC(_mb), 0)
                        msgs_now.append((hwnd, _get_text(hwnd), ok_btn))
                    return True

                user32.EnumWindows(WNDENUMPROC(_find_msgs), 0)
                if msgs_now:
                    for m_hwnd, m_title, ok_btn in msgs_now:
                        mensagens_confirmadas.append(m_title or "Informação")
                        _log(f"💬 Diálogo GIS Eyes: '{m_title}' -> clicando OK...")
                        if ok_btn:
                            _click(ok_btn)
                        else:
                            user32.PostMessageW(m_hwnd, WM_KEYDOWN, 0x0D, 0)
                    time.sleep(1.5)
                    extra_open: List[int] = []

                    def _chk_extra(hw, _lp):
                        p2 = _wt.DWORD()
                        user32.GetWindowThreadProcessId(hw, ctypes.byref(p2))
                        if p2.value == pid and user32.IsWindowVisible(hw) and _get_cls(hw) == "TbsMessageForm":
                            extra_open.append(hw)
                        return True

                    user32.EnumWindows(WNDENUMPROC(_chk_extra), 0)
                    if not extra_open:
                        break

            if not mensagens_confirmadas:
                raise RuntimeError("Tempo esgotado aguardando confirmação final após clicar em Processar no GIS Eyes.")

            _log(f"✅ Setor atualizado com sucesso no GIS Eyes (Operação: {operacao_nome})!")
            return {
                "sucesso": True,
                "operacao": operacao_nome,
                "shp_arquivo": os.path.basename(shp_path),
                "mensagens": mensagens_confirmadas,
            }
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


class AgenteGisEyesHandler(BaseHTTPRequestHandler):
    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin")

    def _send_json(self, obj: dict, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/health":
            exe = localizar_clientetvt4_exe()
            self._send_json({
                "ok": bool(exe),
                "exe": exe,
                "platform": sys.platform,
            })
            return
        self._send_json({"ok": False, "erro": "Rota não encontrada"}, status=404)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/atualizar-setor":
            self._send_json({"sucesso": False, "erro": "Rota não encontrada"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            dados = json.loads(body.decode("utf-8"))
            zip_b64 = str(dados.get("zip_setor_base64") or "").strip()
            contrato_id = str(dados.get("contrato_id") or "").lower().strip()
            if not zip_b64 or not contrato_id:
                raise ValueError("zip_setor_base64 e contrato_id são obrigatórios.")
            zip_bytes = base64.b64decode(zip_b64)
            logs: List[str] = []
            res = atualizar_setor_clientetvt4(
                zip_setor_bytes=zip_bytes,
                contrato_id=contrato_id,
                log_cb=lambda m: (logs.append(m), print(f"[GIS Eyes] {m}", flush=True)),
            )
            self._send_json({"sucesso": True, "resultado": res, "logs": logs})
        except Exception as exc:
            print(f"[GIS Eyes ERRO] {exc}", flush=True)
            self._send_json({"sucesso": False, "erro": str(exc)}, status=500)

    def log_message(self, format: str, *args) -> None:
        print(f"[AGENTE GIS EYES] {self.address_string()} - {format % args}", flush=True)


def main() -> None:
    exe = localizar_clientetvt4_exe()
    print("=" * 68, flush=True)
    print(" LOCAR - AGENTE LOCAL GIS EYES (clientetvt4.exe)", flush=True)
    print(f" Escutando em: http://127.0.0.1:{PORT}", flush=True)
    print(f" Executável  : {exe or 'NÃO ENCONTRADO (extraia GisEyes em Downloads)'}", flush=True)
    print(" Mantenha esta janela aberta ao publicar rotas pelo Sistema LC web.", flush=True)
    print("=" * 68, flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), AgenteGisEyesHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
