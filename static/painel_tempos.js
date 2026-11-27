/* ==========================================================================
   PAINEL COMPLETO DE GESTÃO DE TEMPOS & OPERAÇÃO (painel_tempos.js)
   - Integrado ao Link Compartilhado da Cidade (isolamento estrito) e Admin
   - Filtro por Dia Único ou Mês Consolidado
   - Relação Setores x Caminhões, Garagem, Trajetos (5 tipos), Aterro, Gantt 24h e Excel
   ========================================================================== */
(function () {
    'use strict';

    var state = {
        aberto: false,
        modo: 'dia',           // 'dia' | 'mes'
        cidade: 'cabo',
        data: '',
        mes: '',
        abaAtiva: 'setores',   // 'setores' | 'caminhoes' | 'timeline' | 'ciclo' | 'insights'
        ganttModo: 'caminhao', // 'caminhao' | 'setor'
        turnoFiltro: 'all',
        setorFiltro: 'all',
        placaFiltro: 'all',
        busca: '',
        sortSetor: { col: 'codigo', dir: 1 },
        sortCam: { col: 'tempo_setor_s', dir: -1 },
        expandSetor: {},
        expandCam: {},
        jobId: null,
        pollTimer: null,
        dados: null,
        mesBackup: null        // guarda contexto mensal ao fazer drill-down de 1 dia
    };

    var COR_ESTADO = {
        SETOR: '#00A859',
        TRAJETO: '#2563EB',
        ATERRO: '#D97706',
        GARAGEM: '#64748B',
        REFEICAO: '#8B5CF6',
        SOS: '#DC2626',
        PARADA_EXTERNA: '#F43F5E'
    };

    var NOME_ESTADO = {
        SETOR: 'Em Setor (Coleta)',
        TRAJETO: 'Trajeto / Deslocamento',
        ATERRO: 'Aterro / Descarga',
        GARAGEM: 'Garagem / Base',
        REFEICAO: 'Refeição',
        SOS: 'SOS / Manutenção',
        PARADA_EXTERNA: 'Parada Externa >5m'
    };

    function $(id) { return document.getElementById(id); }
    function esc(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }
    function fmtDur(seg) {
        var s = Math.max(0, Math.round(Number(seg || 0)));
        var h = Math.floor(s / 3600);
        var m = Math.floor((s % 3600) / 60);
        return String(h).padStart(2, '0') + 'h' + String(m).padStart(2, '0');
    }
    function fmtDurMin(seg) {
        var s = Math.max(0, Math.round(Number(seg || 0)));
        if (s < 3600) return Math.round(s / 60) + ' min';
        return fmtDur(s);
    }
    function fmtPeso(kg) {
        var k = Number(kg || 0);
        if (k <= 0) return '0 kg';
        if (k >= 1000) return (k / 1000).toFixed(2).replace('.', ',') + ' t';
        return Math.round(k) + ' kg';
    }
    function hojeISO() {
        var d = new Date();
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }
    function ontemISO() {
        var d = new Date(Date.now() - 86400000);
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }
    function mesAtualISO() {
        return hojeISO().slice(0, 7);
    }
    function mesPassadoISO() {
        var d = new Date();
        d.setDate(1);
        d.setMonth(d.getMonth() - 1);
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    }

    function getAuthInfo() {
        var lc = window.__SISTEMA_LC__;
        if (!lc) return { isShared: false, snapshot_id: '', token: '', cidade: 'cabo' };
        var shared = lc.isSharedLink && lc.isSharedLink();
        var auth = (lc.getSharedAuth && lc.getSharedAuth()) || {};
        return {
            isShared: Boolean(shared),
            snapshot_id: auth.snapshot_id || '',
            token: auth.token || '',
            cidade: auth.cidade || (lc.getCidadeAtual ? lc.getCidadeAtual() : 'cabo')
        };
    }

    function nomeCidade(cid) {
        var lc = window.__SISTEMA_LC__;
        var mapa = (lc && lc.getCidadesMapa && lc.getCidadesMapa()) || {};
        return mapa[cid] || (cid ? cid.toUpperCase() : 'Cidade');
    }

    function garantirOverlayDOM() {
        if ($('painelTemposOverlay')) return;
        var el = document.createElement('div');
        el.id = 'painelTemposOverlay';
        el.className = 'pt-overlay';
        el.innerHTML = [
            '<div class="pt-topbar">',
            '  <div class="pt-brand">',
            '    <div class="pt-brand-icon">⏱️</div>',
            '    <div class="pt-brand-text">',
            '      <h2>Painel Completo de Tempos & Operação</h2>',
            '      <p id="ptSubtituloTopo">Relação completa de setores, caminhões, trajetos, garagem e aterro</p>',
            '    </div>',
            '    <span id="ptCityLockPill" class="pt-city-lock-pill" style="display:none;"></span>',
            '  </div>',
            '  <div class="pt-topbar-actions">',
            '    <button type="button" class="pt-btn" id="ptBtnVoltarMes" style="display:none;background:#EFF6FF;color:#1D4ED8;border-color:#BFDBFE;">⬅ Voltar ao Mês</button>',
            '    <button type="button" class="pt-btn" id="ptBtnAtualizar">🔄 Atualizar</button>',
            '    <button type="button" class="pt-btn pt-btn-excel" id="ptBtnExportarExcel" disabled>📥 Exportar Excel (.xlsx)</button>',
            '    <button type="button" class="pt-btn pt-btn-close" id="ptBtnFechar">✕ Voltar ao Mapa</button>',
            '  </div>',
            '</div>',
            '<div class="pt-filterbar">',
            '  <div class="pt-filter-group-row">',
            '    <div class="pt-field" id="ptWrapSelectCidade">',
            '      <label for="ptSelectCidade">Cidade</label>',
            '      <select id="ptSelectCidade"></select>',
            '    </div>',
            '    <div class="pt-segmented">',
            '      <button type="button" class="pt-seg-btn active" data-modo="dia" id="ptModoDiaBtn">📅 Por Dia</button>',
            '      <button type="button" class="pt-seg-btn" data-modo="mes" id="ptModoMesBtn">🗓️ Por Mês</button>',
            '    </div>',
            '    <div class="pt-field" id="ptWrapData">',
            '      <label for="ptInputData">Data</label>',
            '      <input type="date" id="ptInputData" />',
            '    </div>',
            '    <div class="pt-field" id="ptWrapMes" style="display:none;">',
            '      <label for="ptInputMes">Mês</label>',
            '      <input type="month" id="ptInputMes" />',
            '    </div>',
            '    <div class="pt-quick-pills" id="ptQuickPills">',
            '      <button type="button" class="pt-quick-btn" data-q="hoje">Hoje</button>',
            '      <button type="button" class="pt-quick-btn" data-q="ontem">Ontem</button>',
            '      <button type="button" class="pt-quick-btn" data-q="mes_atual">Este Mês</button>',
            '      <button type="button" class="pt-quick-btn" data-q="mes_ant">Mês Passado</button>',
            '    </div>',
            '    <button type="button" class="pt-btn pt-btn-primary" id="ptBtnConsultar">🔍 Carregar</button>',
            '  </div>',
            '  <div class="pt-filter-group-row">',
            '    <div class="pt-field">',
            '      <label for="ptFilterTurno">Turno</label>',
            '      <select id="ptFilterTurno"><option value="all">Todos</option><option value="DIURNO">☀️ Diurno</option><option value="NOTURNO">🌙 Noturno</option></select>',
            '    </div>',
            '    <div class="pt-field">',
            '      <label for="ptFilterSetor">Setor</label>',
            '      <select id="ptFilterSetor"><option value="all">Todos os setores</option></select>',
            '    </div>',
            '    <div class="pt-field">',
            '      <label for="ptFilterPlaca">Caminhão</label>',
            '      <select id="ptFilterPlaca"><option value="all">Todos</option></select>',
            '    </div>',
            '    <div class="pt-field">',
            '      <input type="text" id="ptBuscaTexto" placeholder="🔎 Buscar setor, placa ou motorista..." style="width:185px;" />',
            '    </div>',
            '  </div>',
            '</div>',
            '<div class="pt-body" id="ptBody"></div>'
        ].join('');
        document.body.appendChild(el);
        bindEventosPainel();
    }

    function bindEventosPainel() {
        $('ptBtnFechar').addEventListener('click', fecharPainel);
        $('ptBtnAtualizar').addEventListener('click', function () { carregarDados(true); });
        $('ptBtnConsultar').addEventListener('click', function () { state.mesBackup = null; $('ptBtnVoltarMes').style.display = 'none'; carregarDados(false); });
        $('ptBtnExportarExcel').addEventListener('click', exportarExcel);
        $('ptBtnVoltarMes').addEventListener('click', function () {
            if (!state.mesBackup) return;
            state.modo = 'mes';
            state.mes = state.mesBackup.mes;
            state.dados = state.mesBackup.dados;
            state.jobId = state.mesBackup.jobId;
            state.mesBackup = null;
            $('ptBtnVoltarMes').style.display = 'none';
            sincronizarControlesTopo();
            popularFiltrosLocais();
            renderizarTudo();
        });

        $('ptModoDiaBtn').addEventListener('click', function () { trocarModo('dia'); });
        $('ptModoMesBtn').addEventListener('click', function () { trocarModo('mes'); });

        document.querySelectorAll('#ptQuickPills .pt-quick-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var q = btn.getAttribute('data-q');
                state.mesBackup = null;
                $('ptBtnVoltarMes').style.display = 'none';
                if (q === 'hoje') { state.modo = 'dia'; state.data = hojeISO(); }
                else if (q === 'ontem') { state.modo = 'dia'; state.data = ontemISO(); }
                else if (q === 'mes_atual') { state.modo = 'mes'; state.mes = mesAtualISO(); }
                else if (q === 'mes_ant') { state.modo = 'mes'; state.mes = mesPassadoISO(); }
                sincronizarControlesTopo();
                carregarDados(false);
            });
        });

        $('ptSelectCidade').addEventListener('change', function () {
            state.cidade = this.value;
            state.mesBackup = null;
            $('ptBtnVoltarMes').style.display = 'none';
            carregarDados(false);
        });
        $('ptInputData').addEventListener('change', function () {
            state.data = this.value;
            state.mesBackup = null;
            $('ptBtnVoltarMes').style.display = 'none';
        });
        $('ptInputMes').addEventListener('change', function () {
            state.mes = this.value;
            state.mesBackup = null;
            $('ptBtnVoltarMes').style.display = 'none';
        });

        $('ptFilterTurno').addEventListener('change', function () { state.turnoFiltro = this.value; renderizarConteudoAba(); });
        $('ptFilterSetor').addEventListener('change', function () { state.setorFiltro = this.value; renderizarConteudoAba(); });
        $('ptFilterPlaca').addEventListener('change', function () { state.placaFiltro = this.value; renderizarConteudoAba(); });
        $('ptBuscaTexto').addEventListener('input', function () { state.busca = this.value.toLowerCase().trim(); renderizarConteudoAba(); });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && state.aberto) fecharPainel();
        });
    }

    function trocarModo(m) {
        state.modo = m === 'mes' ? 'mes' : 'dia';
        state.mesBackup = null;
        $('ptBtnVoltarMes').style.display = 'none';
        sincronizarControlesTopo();
        carregarDados(false);
    }

    function sincronizarControlesTopo() {
        var auth = getAuthInfo();
        var selCid = $('ptSelectCidade');
        var wrapCid = $('ptWrapSelectCidade');
        var lockPill = $('ptCityLockPill');

        if (auth.isShared) {
            state.cidade = auth.cidade || state.cidade;
            wrapCid.style.display = 'none';
            lockPill.style.display = 'inline-flex';
            lockPill.textContent = '🔒 Cidade Exclusiva: ' + nomeCidade(state.cidade);
            $('ptBtnAtualizar').style.display = 'none';
        } else {
            lockPill.style.display = 'none';
            wrapCid.style.display = 'flex';
            $('ptBtnAtualizar').style.display = 'inline-flex';
            if (selCid.options.length === 0) {
                var lc = window.__SISTEMA_LC__;
                var mapa = (lc && lc.getCidadesMapa && lc.getCidadesMapa()) || {};
                Object.keys(mapa).forEach(function (k) {
                    if (k === 'all') return;
                    var opt = document.createElement('option');
                    opt.value = k;
                    opt.textContent = mapa[k];
                    selCid.appendChild(opt);
                });
            }
            if (!state.cidade || state.cidade === 'all') {
                state.cidade = selCid.options.length ? selCid.options[0].value : 'cabo';
            }
            selCid.value = state.cidade;
        }

        $('ptModoDiaBtn').classList.toggle('active', state.modo === 'dia');
        $('ptModoMesBtn').classList.toggle('active', state.modo === 'mes');
        $('ptWrapData').style.display = state.modo === 'dia' ? 'flex' : 'none';
        $('ptWrapMes').style.display = state.modo === 'mes' ? 'flex' : 'none';

        if (!state.data) state.data = hojeISO();
        if (!state.mes) state.mes = state.data.slice(0, 7) || mesAtualISO();
        $('ptInputData').value = state.data;
        $('ptInputMes').value = state.mes;
        $('ptInputData').max = hojeISO();
        $('ptInputMes').max = mesAtualISO();
    }

    function abrirPainel() {
        garantirOverlayDOM();
        var lc = window.__SISTEMA_LC__;
        var auth = getAuthInfo();
        var cidAtual = auth.isShared ? auth.cidade : ((lc && lc.getCidadeAtual && lc.getCidadeAtual()) || 'cabo');
        if (!cidAtual || cidAtual === 'all') cidAtual = 'cabo';
        var dtAtual = (lc && lc.getDataAtual && lc.getDataAtual()) || hojeISO();

        state.cidade = cidAtual;
        state.data = dtAtual;
        state.mes = dtAtual.slice(0, 7);
        state.mesBackup = null;
        $('ptBtnVoltarMes').style.display = 'none';

        // Se o link foi gerado no modo mensal/quinzenal, abre por padrão no mês daquela data
        var shInfo = (lc && lc.getSharedInfo && lc.getSharedInfo()) || {};
        if (auth.isShared && shInfo.periodo === 'mensal') {
            state.modo = 'mes';
        } else {
            state.modo = 'dia';
        }

        sincronizarControlesTopo();
        $('painelTemposOverlay').classList.add('pt-open');
        state.aberto = true;
        carregarDados(false);
    }

    function fecharPainel() {
        if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
        var ov = $('painelTemposOverlay');
        if (ov) ov.classList.remove('pt-open');
        state.aberto = false;
    }

    function mostrarCarregamento(pct, label, sub) {
        $('ptBody').innerHTML = [
            '<div class="pt-loading-box">',
            '  <div style="font-size:1.5rem;margin-bottom:4px;">⏱️</div>',
            '  <div style="font-size:0.95rem;font-weight:800;color:#0F172A;">' + esc(label) + '</div>',
            '  <div class="pt-prog-track"><div class="pt-prog-bar" style="width:' + Math.max(4, pct) + '%"></div></div>',
            '  <div style="font-size:0.75rem;color:#64748B;">' + esc(sub) + ' (' + Math.round(pct) + '%)</div>',
            '</div>'
        ].join('');
    }

    async function carregarDados(forcar) {
        if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
        $('ptBtnExportarExcel').disabled = true;
        state.dados = null;

        var auth = getAuthInfo();
        var refTxt = state.modo === 'mes' ? ('mês ' + state.mes) : ('dia ' + state.data);
        mostrarCarregamento(8, 'Processando métricas completas de tempo — ' + nomeCidade(state.cidade), 'Analisando setores, caminhões, trajetos, garagem e aterro (' + refTxt + ')...');

        try {
            var resp = await fetch('/api/painel-tempos/iniciar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cidade: state.cidade,
                    modo: state.modo,
                    data: state.data,
                    mes: state.mes,
                    forcar: Boolean(forcar),
                    snapshot_id: auth.snapshot_id,
                    token: auth.token
                })
            });
            var res = await resp.json();
            if (!res || !res.sucesso) {
                $('ptBody').innerHTML = '<div class="pt-loading-box" style="border-color:#FCA5A5;"><b style="color:#B91C1C;">⚠️ ' + esc((res && res.erro) || 'Falha ao processar painel') + '</b></div>';
                return;
            }
            state.jobId = res.job_id;
            if (res.concluido_imediato && res.resultado) {
                aoConcluirDados(res.resultado);
                return;
            }

            state.pollTimer = setInterval(async function () {
                try {
                    var qUrl = '/api/painel-tempos/status?job_id=' + encodeURIComponent(state.jobId);
                    if (auth.snapshot_id) qUrl += '&snapshot_id=' + encodeURIComponent(auth.snapshot_id);
                    if (auth.token) qUrl += '&token=' + encodeURIComponent(auth.token);
                    var rSt = await fetch(qUrl);
                    var st = await rSt.json();
                    if (!st || !st.sucesso) {
                        clearInterval(state.pollTimer); state.pollTimer = null;
                        $('ptBody').innerHTML = '<div class="pt-loading-box"><b>⚠️ ' + esc((st && st.erro) || 'Erro na consulta') + '</b></div>';
                        return;
                    }
                    if (st.status === 'done' && st.resultado) {
                        clearInterval(state.pollTimer); state.pollTimer = null;
                        aoConcluirDados(st.resultado);
                    } else if (st.status === 'error') {
                        clearInterval(state.pollTimer); state.pollTimer = null;
                        $('ptBody').innerHTML = '<div class="pt-loading-box"><b style="color:#B91C1C;">❌ ' + esc(st.erro || 'Erro ao consolidar mês') + '</b></div>';
                    } else {
                        mostrarCarregamento(
                            Number(st.progresso_pct || 10),
                            'Consolidando ' + nomeCidade(state.cidade) + ' (' + (st.etapas_concluidas || 0) + '/' + (st.etapas_total || 1) + ' dias)',
                            'Processando dia ' + (st.data_atual || '...') + ' — cruzando GPS, setores, garagem e aterro'
                        );
                    }
                } catch (_) {}
            }, 900);
        } catch (err) {
            $('ptBody').innerHTML = '<div class="pt-loading-box"><b style="color:#B91C1C;">Erro de comunicação com o servidor.</b></div>';
        }
    }

    function aoConcluirDados(resultado) {
        state.dados = resultado;
        state.turnoFiltro = 'all';
        state.setorFiltro = 'all';
        state.placaFiltro = 'all';
        state.busca = '';
        $('ptFilterTurno').value = 'all';
        $('ptBuscaTexto').value = '';
        $('ptBtnExportarExcel').disabled = false;

        var r = resultado.resumo || {};
        $('ptSubtituloTopo').textContent = (r.contrato_nome || nomeCidade(state.cidade)) + ' · ' + (r.data_formatada || '') +
            ' · ' + (r.caminhoes_ativos || 0) + ' caminhão(ões) · ' + (r.setores_executados || 0) + '/' + (r.setores_previstos || 0) + ' setores';

        popularFiltrosLocais();
        renderizarTudo();
    }

    function popularFiltrosLocais() {
        var d = state.dados;
        if (!d) return;
        var selSet = $('ptFilterSetor');
        var selPl = $('ptFilterPlaca');
        selSet.innerHTML = '<option value="all">Todos os setores (' + (d.setores || []).length + ')</option>';
        (d.setores || []).forEach(function (s) {
            var opt = document.createElement('option');
            opt.value = s.codigo;
            opt.textContent = s.codigo + ' (' + fmtDur(s.tempo_efetivo_s) + ')';
            selSet.appendChild(opt);
        });
        selPl.innerHTML = '<option value="all">Todos os caminhões (' + (d.caminhoes || []).length + ')</option>';
        (d.caminhoes || []).forEach(function (c) {
            var opt = document.createElement('option');
            opt.value = c.placa;
            opt.textContent = c.placa + ' · ' + fmtDur(c.jornada_total_s);
            selPl.appendChild(opt);
        });
    }

    function exportarExcel() {
        if (!state.jobId) return;
        var auth = getAuthInfo();
        var url = '/api/painel-tempos/exportar-excel?job_id=' + encodeURIComponent(state.jobId);
        if (auth.snapshot_id) url += '&snapshot_id=' + encodeURIComponent(auth.snapshot_id);
        if (auth.token) url += '&token=' + encodeURIComponent(auth.token);
        window.location.href = url;
    }

    function filtrarSetores() {
        var lista = (state.dados && state.dados.setores) || [];
        return lista.filter(function (s) {
            if (state.turnoFiltro !== 'all' && s.turno !== state.turnoFiltro) return false;
            if (state.setorFiltro !== 'all' && s.codigo !== state.setorFiltro) return false;
            if (state.placaFiltro !== 'all') {
                var tem = (s.caminhoes || []).some(function (c) { return c.placa === state.placaFiltro; });
                if (!tem) return false;
            }
            if (state.busca) {
                var hay = (s.codigo + ' ' + (s.placa_principal || '') + ' ' + (s.caminhoes || []).map(function (c) { return c.placa + ' ' + c.motorista; }).join(' ')).toLowerCase();
                if (hay.indexOf(state.busca) < 0) return false;
            }
            return true;
        });
    }

    function filtrarCaminhoes() {
        var lista = (state.dados && state.dados.caminhoes) || [];
        return lista.filter(function (c) {
            if (state.turnoFiltro !== 'all' && c.turno !== state.turnoFiltro) return false;
            if (state.placaFiltro !== 'all' && c.placa !== state.placaFiltro) return false;
            if (state.setorFiltro !== 'all') {
                var tem = (c.setores_atendidos || []).some(function (sa) { return sa.setor === state.setorFiltro; });
                if (!tem) return false;
            }
            if (state.busca) {
                var hay = (c.placa + ' ' + (c.motorista || '') + ' ' + (c.setores_atendidos || []).map(function (sa) { return sa.setor; }).join(' ')).toLowerCase();
                if (hay.indexOf(state.busca) < 0) return false;
            }
            return true;
        });
    }

    function filtrarViagensAterro() {
        var lista = (state.dados && state.dados.viagens_aterro) || [];
        return lista.filter(function (v) {
            if (state.placaFiltro !== 'all' && v.placa !== state.placaFiltro) return false;
            if (state.setorFiltro !== 'all' && v.setor_origem !== state.setorFiltro) return false;
            if (state.busca) {
                var hay = ((v.placa || '') + ' ' + (v.motorista || '') + ' ' + (v.setor_origem || '') + ' ' + (v.ticket || '')).toLowerCase();
                if (hay.indexOf(state.busca) < 0) return false;
            }
            return true;
        });
    }

    function renderizarTudo() {
        var d = state.dados;
        if (!d) return;
        var r = d.resumo || {};
        var isMes = r.modo === 'mes';

        var kpisHtml = [
            '<div class="pt-kpi-grid">',
            '  <div class="pt-kpi-card">',
            '    <span class="pt-kpi-label">Jornada Operacional Total</span>',
            '    <span class="pt-kpi-val">' + fmtDur(r.jornada_total_s) + '</span>',
            '    <span class="pt-kpi-sub">Média/caminhão: <b>' + fmtDur(r.jornada_media_caminhao_s) + '</b> · ' + (isMes ? (r.dias_operacionais + ' dias op.') : ('1ª saída ' + (r.primeira_saida_garagem || '--:--') + ' → ' + (r.ultimo_retorno_garagem || '--:--'))) + '</span>',
            '  </div>',
            '  <div class="pt-kpi-card c-emerald">',
            '    <span class="pt-kpi-label">Tempo Efetivo em Setor</span>',
            '    <span class="pt-kpi-val" style="color:#059669;">' + fmtDur(r.tempo_setor_total_s) + ' <small style="font-size:0.78rem;">(' + r.pct_tempo_setor + '%)</small></span>',
            '    <span class="pt-kpi-sub">Movimento: <b>' + fmtDur(r.tempo_setor_mov_s) + '</b> · Parado: <b>' + fmtDur(r.tempo_setor_parado_s) + '</b></span>',
            '  </div>',
            '  <div class="pt-kpi-card c-cyan">',
            '    <span class="pt-kpi-label">Duração Média por Setor</span>',
            '    <span class="pt-kpi-val">' + fmtDur(r.tempo_efetivo_medio_setor_s) + '</span>',
            '    <span class="pt-kpi-sub">Janela (Início→Fim): <b>' + fmtDur(r.duracao_janela_media_setor_s) + '</b> · ' + r.setores_executados + '/' + r.setores_previstos + ' setores (' + r.pct_cobertura_geral + '%)</span>',
            '  </div>',
            '  <div class="pt-kpi-card c-blue">',
            '    <span class="pt-kpi-label">Tempo Total de Trajeto</span>',
            '    <span class="pt-kpi-val" style="color:#2563EB;">' + fmtDur(r.tempo_trajeto_total_s) + ' <small style="font-size:0.78rem;">(' + r.pct_tempo_trajeto + '%)</small></span>',
            '    <span class="pt-kpi-sub">Média/caminhão: <b>' + fmtDur(r.tempo_trajeto_medio_caminhao_s) + '</b> · Vel. média: <b>' + r.vel_media_trajeto_kmh + ' km/h</b></span>',
            '  </div>',
            '  <div class="pt-kpi-card c-slate">',
            '    <span class="pt-kpi-label">Tempo na Garagem</span>',
            '    <span class="pt-kpi-val">' + fmtDur(r.tempo_garagem_total_s) + ' <small style="font-size:0.78rem;">(' + r.pct_tempo_garagem + '%)</small></span>',
            '    <span class="pt-kpi-sub">Pré-saída: <b>' + fmtDur(r.garagem_pre_saida_s) + '</b> · Pós-retorno: <b>' + fmtDur(r.garagem_pos_retorno_s) + '</b></span>',
            '  </div>',
            '  <div class="pt-kpi-card c-amber">',
            '    <span class="pt-kpi-label">Tempo no Aterro / Descarga</span>',
            '    <span class="pt-kpi-val" style="color:#D97706;">' + fmtDur(r.tempo_aterro_total_s) + ' <small style="font-size:0.78rem;">(' + r.pct_tempo_aterro + '%)</small></span>',
            '    <span class="pt-kpi-sub"><b>' + r.qtd_viagens_aterro + '</b> viagens · Média: <b>' + fmtDurMin(r.tempo_medio_por_descarga_s) + '</b> · Peso: <b>' + fmtPeso(r.peso_total_kg) + '</b></span>',
            '  </div>',
            '  <div class="pt-kpi-card c-rose">',
            '    <span class="pt-kpi-label">Refeição, SOS & Paradas</span>',
            '    <span class="pt-kpi-val" style="color:#E11D48;">' + fmtDur(r.tempo_paradas_total_s) + ' <small style="font-size:0.78rem;">(' + r.pct_tempo_paradas + '%)</small></span>',
            '    <span class="pt-kpi-sub">Refeição: <b>' + fmtDur(r.tempo_refeicao_total_s) + '</b> · SOS: <b>' + fmtDur(r.tempo_sos_total_s) + '</b> · Outras: <b>' + fmtDur(r.tempo_parada_externa_total_s) + '</b></span>',
            '  </div>',
            '  <div class="pt-kpi-card c-purple">',
            '    <span class="pt-kpi-label">Ritmo & Velocidade de Coleta</span>',
            '    <span class="pt-kpi-val">' + r.vel_media_coleta_kmh + ' <small style="font-size:0.78rem;">km/h</small></span>',
            '    <span class="pt-kpi-sub">Ritmo: <b>' + r.ritmo_medio_min_por_km + ' min/km</b> · Prod: <b>' + r.produtividade_media_km_h + ' km/h</b> · Rodado: <b>' + r.km_rodado_total + ' km</b></span>',
            '  </div>',
            '</div>'
        ].join('');

        var compHtml = [
            '<div class="pt-comp-card">',
            '  <div class="pt-comp-header">',
            '    <span class="pt-comp-title">Distribuição Completa do Tempo da Operação (100% da Jornada)</span>',
            '    <span style="font-size:0.72rem;color:#475569;">Saída média Diurno: <b>' + esc(r.hora_media_saida_diurno || '--:--') + '</b> · Noturno: <b>' + esc(r.hora_media_saida_noturno || '--:--') + '</b></span>',
            '  </div>',
            '  <div class="pt-comp-bar">',
            '    <div class="pt-comp-seg" style="width:' + r.pct_tempo_setor + '%;background:' + COR_ESTADO.SETOR + ';" title="Em Setor: ' + fmtDur(r.tempo_setor_total_s) + '">' + (r.pct_tempo_setor >= 7 ? r.pct_tempo_setor + '%' : '') + '</div>',
            '    <div class="pt-comp-seg" style="width:' + r.pct_tempo_trajeto + '%;background:' + COR_ESTADO.TRAJETO + ';" title="Trajeto: ' + fmtDur(r.tempo_trajeto_total_s) + '">' + (r.pct_tempo_trajeto >= 7 ? r.pct_tempo_trajeto + '%' : '') + '</div>',
            '    <div class="pt-comp-seg" style="width:' + r.pct_tempo_aterro + '%;background:' + COR_ESTADO.ATERRO + ';" title="Aterro: ' + fmtDur(r.tempo_aterro_total_s) + '">' + (r.pct_tempo_aterro >= 6 ? r.pct_tempo_aterro + '%' : '') + '</div>',
            '    <div class="pt-comp-seg" style="width:' + r.pct_tempo_garagem + '%;background:' + COR_ESTADO.GARAGEM + ';" title="Garagem: ' + fmtDur(r.tempo_garagem_total_s) + '">' + (r.pct_tempo_garagem >= 6 ? r.pct_tempo_garagem + '%' : '') + '</div>',
            '    <div class="pt-comp-seg" style="width:' + r.pct_tempo_paradas + '%;background:' + COR_ESTADO.PARADA_EXTERNA + ';" title="Paradas/Ref/SOS: ' + fmtDur(r.tempo_paradas_total_s) + '">' + (r.pct_tempo_paradas >= 5 ? r.pct_tempo_paradas + '%' : '') + '</div>',
            '  </div>',
            '  <div class="pt-comp-legend">',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.SETOR + ';"></span>Em Setor: <b>' + fmtDur(r.tempo_setor_total_s) + '</b> (' + r.pct_tempo_setor + '%)</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.TRAJETO + ';"></span>Trajeto: <b>' + fmtDur(r.tempo_trajeto_total_s) + '</b> (' + r.pct_tempo_trajeto + '%)</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.ATERRO + ';"></span>Aterro: <b>' + fmtDur(r.tempo_aterro_total_s) + '</b> (' + r.pct_tempo_aterro + '%)</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.GARAGEM + ';"></span>Garagem: <b>' + fmtDur(r.tempo_garagem_total_s) + '</b> (' + r.pct_tempo_garagem + '%)</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.PARADA_EXTERNA + ';"></span>Refeição/SOS/Paradas: <b>' + fmtDur(r.tempo_paradas_total_s) + '</b> (' + r.pct_tempo_paradas + '%)</span>',
            '  </div>',
            '  <div class="pt-subtraj-row">',
            '    <span style="font-size:0.71rem;font-weight:800;color:#475569;align-self:center;">DETALHE DOS TRAJETOS:</span>',
            '    <span class="pt-subtraj-pill">Garagem → Setor: <strong>' + fmtDur(r.trajeto_garagem_setor_s) + '</strong></span>',
            '    <span class="pt-subtraj-pill">Setor → Aterro: <strong>' + fmtDur(r.trajeto_setor_aterro_s) + '</strong></span>',
            '    <span class="pt-subtraj-pill">Aterro → Setor: <strong>' + fmtDur(r.trajeto_aterro_setor_s) + '</strong></span>',
            '    <span class="pt-subtraj-pill">Entre Setores: <strong>' + fmtDur(r.trajeto_entre_setores_s) + '</strong></span>',
            '    <span class="pt-subtraj-pill">Retorno → Garagem: <strong>' + fmtDur(r.trajeto_retorno_garagem_s) + '</strong></span>',
            '  </div>',
            '</div>'
        ].join('');

        var tabsHtml = [
            '<div class="pt-tabs">',
            '  <button type="button" class="pt-tab-btn ' + (state.abaAtiva === 'setores' ? 'active' : '') + '" data-tab="setores">🗺️ Relação de Setores & Caminhões</button>',
            '  <button type="button" class="pt-tab-btn ' + (state.abaAtiva === 'caminhoes' ? 'active' : '') + '" data-tab="caminhoes">🚛 Frota & Jornada Completa</button>',
            '  <button type="button" class="pt-tab-btn ' + (state.abaAtiva === 'timeline' ? 'active' : '') + '" data-tab="timeline">' + (isMes ? '📈 Evolução Diária do Mês' : '⏱️ Linha do Tempo Gantt 24h') + '</button>',
            '  <button type="button" class="pt-tab-btn ' + (state.abaAtiva === 'ciclo' ? 'active' : '') + '" data-tab="ciclo">⚖️ Garagem, Trajetos & Aterro</button>',
            '  <button type="button" class="pt-tab-btn ' + (state.abaAtiva === 'insights' ? 'active' : '') + '" data-tab="insights">💡 Curva Horária & Insights</button>',
            '</div>',
            '<div id="ptTabContent"></div>'
        ].join('');

        $('ptBody').innerHTML = kpisHtml + compHtml + tabsHtml;

        document.querySelectorAll('.pt-tab-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                state.abaAtiva = btn.getAttribute('data-tab');
                document.querySelectorAll('.pt-tab-btn').forEach(function (b) { b.classList.toggle('active', b === btn); });
                renderizarConteudoAba();
            });
        });

        renderizarConteudoAba();
    }

    function ordenarLista(arr, col, dir) {
        return arr.slice().sort(function (a, b) {
            var va = a[col], vb = b[col];
            if (va == null) va = '';
            if (vb == null) vb = '';
            if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
            return String(va).localeCompare(String(vb), 'pt-BR', { numeric: true }) * dir;
        });
    }

    function renderizarConteudoAba() {
        var box = $('ptTabContent');
        if (!box || !state.dados) return;
        if (state.abaAtiva === 'setores') renderAbaSetores(box);
        else if (state.abaAtiva === 'caminhoes') renderAbaCaminhoes(box);
        else if (state.abaAtiva === 'timeline') renderAbaTimeline(box);
        else if (state.abaAtiva === 'ciclo') renderAbaCiclo(box);
        else renderAbaInsights(box);
    }

    function renderAbaSetores(box) {
        var isMes = state.dados.resumo && state.dados.resumo.modo === 'mes';
        var setores = ordenarLista(filtrarSetores(), state.sortSetor.col, state.sortSetor.dir);

        var rowsHtml = setores.map(function (s) {
            var exp = Boolean(state.expandSetor[s.codigo]);
            var badgeSt = s.status_execucao === 'CONCLUIDO'
                ? '<span class="pt-badge pt-badge-ok">Concluído</span>'
                : (s.status_execucao === 'PARCIAL' ? '<span class="pt-badge pt-badge-warn">Parcial</span>' : '<span class="pt-badge pt-badge-err">Não Iniciado</span>');
            var badgeTur = s.turno === 'NOTURNO' ? '<span class="pt-badge pt-badge-noturno">Noturno</span>' : '<span class="pt-badge pt-badge-diurno">Diurno</span>';
            var camResumo = (s.caminhoes && s.caminhoes.length > 0)
                ? ('<b>' + esc(s.placa_principal) + '</b>' + (s.qtd_caminhoes > 1 ? ' <span class="pt-badge pt-badge-apoio">+' + (s.qtd_caminhoes - 1) + ' apoio</span>' : ''))
                : '<span style="color:#94A3B8;">Sem registro</span>';

            var html = '<tr class="pt-row-main ' + (exp ? 'pt-row-expanded' : '') + '" data-setor="' + esc(s.codigo) + '">' +
                '<td><b>' + (exp ? '▾ ' : '▸ ') + esc(s.codigo) + '</b>' + (isMes ? '<div style="font-size:0.67rem;color:#64748B;">' + s.dias_executados + '/' + s.dias_previstos + ' dias</div>' : '') + '</td>' +
                '<td>' + badgeTur + '</td>' +
                '<td>' + badgeSt + '</td>' +
                '<td>' + esc(s.hora_inicio_hhmm || '--:--') + ' → ' + esc(s.hora_fim_hhmm || '--:--') + '</td>' +
                '<td><b>' + fmtDur(s.duracao_janela_s) + '</b></td>' +
                '<td style="color:#059669;"><b>' + fmtDur(isMes ? s.tempo_efetivo_medio_s : s.tempo_efetivo_s) + '</b>' + (isMes ? '<div style="font-size:0.67rem;color:#64748B;">Total: ' + fmtDur(s.tempo_efetivo_s) + '</div>' : '') + '</td>' +
                '<td>' + fmtDurMin(s.tempo_intervalo_externo_s) + '</td>' +
                '<td>' + fmtDur(s.tempo_movimento_s) + ' / ' + fmtDur(s.tempo_parado_s) + '</td>' +
                '<td><div class="pt-mini-bar-bg"><div class="pt-mini-bar-fill" style="width:' + Math.min(100, s.pct_cobertura) + '%;"></div></div><b>' + s.pct_cobertura + '%</b><div style="font-size:0.67rem;color:#64748B;">' + s.km_coletado + '/' + s.km_previsto + ' km</div></td>' +
                '<td>' + s.ritmo_min_por_km + ' min/km<div style="font-size:0.67rem;color:#64748B;">' + s.produtividade_km_h + ' km/h</div></td>' +
                '<td>' + camResumo + '</td>' +
                '</tr>';

            if (exp) {
                var subRows = (s.caminhoes || []).map(function (c) {
                    return '<tr>' +
                        '<td><b>' + esc(c.placa) + '</b></td>' +
                        '<td><span class="pt-badge ' + (c.eh_principal ? 'pt-badge-pri' : 'pt-badge-apoio') + '">' + esc(c.papel) + '</span></td>' +
                        '<td>' + esc(c.motorista || '-') + '</td>' +
                        '<td>' + (isMes ? (c.dias_no_setor + ' dias') : (esc(c.entrada_hhmm) + ' → ' + esc(c.saida_hhmm))) + '</td>' +
                        '<td style="color:#059669;"><b>' + fmtDur(c.tempo_s) + '</b>' + (isMes ? (' (' + fmtDur(c.tempo_medio_dia_s) + '/dia)') : '') + '</td>' +
                        '<td><div class="pt-mini-bar-bg"><div class="pt-mini-bar-fill" style="width:' + Math.min(100, c.pct_tempo_setor) + '%;"></div></div><b>' + c.pct_tempo_setor + '%</b></td>' +
                        '<td>' + fmtDur(c.mov_s) + ' / ' + fmtDur(c.parado_s) + '</td>' +
                        '<td>' + c.passagens + '</td>' +
                        '<td><b>' + c.km_coletado + ' km</b> (' + c.pct_cobertura_setor + '%)</td>' +
                        '</tr>';
                }).join('');

                html += '<tr class="pt-expand-row"><td colspan="11">' +
                    '<div style="font-size:0.74rem;font-weight:800;color:#0F766E;margin-bottom:6px;">🚛 Caminhões que executaram o setor ' + esc(s.codigo) + ' (quanto tempo cada caminhão ficou no setor):</div>' +
                    (subRows ? ('<div class="pt-table-wrap"><table class="pt-table" style="min-width:650px;"><thead><tr><th>Placa</th><th>Papel</th><th>Motorista</th><th>' + (isMes ? 'Dias' : 'Entrada → Saída') + '</th><th>Tempo no Setor</th><th>% Tempo Setor</th><th>Mov. / Parado</th><th>Passagens</th><th>Km Coletado</th></tr></thead><tbody>' + subRows + '</tbody></table></div>') : '<div style="color:#64748B;font-size:0.74rem;">Nenhum caminhão registrou passagem neste setor no período selecionado.</div>') +
                    '</td></tr>';
            }
            return html;
        }).join('');

        box.innerHTML = [
            '<div class="pt-panel">',
            '  <div class="pt-panel-head">',
            '    <h3>Relação Completa de Setores (' + setores.length + ') — Clique em um setor para ver o tempo de cada caminhão</h3>',
            '    <span style="font-size:0.72rem;color:#64748B;">Duração Janela = Início → Fim | Intervalo Externo = Saídas intermediárias (ex.: Aterro)</span>',
            '  </div>',
            '  <div class="pt-table-wrap"><table class="pt-table" id="ptTblSetores">',
            '    <thead><tr>',
            '      <th data-col="codigo">Setor</th>',
            '      <th data-col="turno">Turno</th>',
            '      <th data-col="status_execucao">Status</th>',
            '      <th data-col="hora_inicio_hhmm">Início → Fim</th>',
            '      <th data-col="duracao_janela_s">Duração Janela</th>',
            '      <th data-col="' + (isMes ? 'tempo_efetivo_medio_s' : 'tempo_efetivo_s') + '">' + (isMes ? 'Tempo Médio/Dia' : 'Tempo Efetivo') + '</th>',
            '      <th data-col="tempo_intervalo_externo_s">Interv. Externo</th>',
            '      <th data-col="tempo_movimento_s">Mov. / Parado</th>',
            '      <th data-col="pct_cobertura">Cobertura</th>',
            '      <th data-col="ritmo_min_por_km">Ritmo / Prod.</th>',
            '      <th data-col="qtd_caminhoes">Caminhões</th>',
            '    </tr></thead>',
            '    <tbody>' + (rowsHtml || '<tr><td colspan="11" style="text-align:center;color:#64748B;padding:24px;">Nenhum setor encontrado com os filtros atuais.</td></tr>') + '</tbody>',
            '  </table></div>',
            '</div>'
        ].join('');

        box.querySelectorAll('#ptTblSetores thead th').forEach(function (th) {
            th.addEventListener('click', function () {
                var col = th.getAttribute('data-col');
                if (state.sortSetor.col === col) state.sortSetor.dir *= -1;
                else { state.sortSetor.col = col; state.sortSetor.dir = 1; }
                renderAbaSetores(box);
            });
        });
        box.querySelectorAll('#ptTblSetores tbody tr.pt-row-main').forEach(function (tr) {
            tr.addEventListener('click', function () {
                var sc = tr.getAttribute('data-setor');
                state.expandSetor[sc] = !state.expandSetor[sc];
                renderAbaSetores(box);
            });
        });
    }

    function renderAbaCaminhoes(box) {
        var isMes = state.dados.resumo && state.dados.resumo.modo === 'mes';
        var caminhoes = ordenarLista(filtrarCaminhoes(), state.sortCam.col, state.sortCam.dir);

        var rowsHtml = caminhoes.map(function (c) {
            var exp = Boolean(state.expandCam[c.placa]);
            var badgeTur = c.turno === 'NOTURNO' ? '<span class="pt-badge pt-badge-noturno">Noturno</span>' : '<span class="pt-badge pt-badge-diurno">Diurno</span>';
            var html = '<tr class="pt-row-main ' + (exp ? 'pt-row-expanded' : '') + '" data-placa="' + esc(c.placa) + '">' +
                '<td><b>' + (exp ? '▾ ' : '▸ ') + esc(c.placa) + '</b><div style="font-size:0.67rem;color:#64748B;">' + esc(c.tipo_veiculo || '') + (isMes ? (' · ' + c.dias_ativos + 'd') : '') + '</div></td>' +
                '<td>' + badgeTur + '</td>' +
                '<td>' + esc(c.motorista || '-') + '</td>' +
                '<td>' + esc(c.saida_garagem_hhmm || '--:--') + ' → ' + esc(c.chegada_garagem_hhmm || '--:--') + '</td>' +
                '<td><b>' + fmtDur(c.jornada_total_s) + '</b>' + (isMes ? ('<div style="font-size:0.67rem;color:#64748B;">' + fmtDur(c.jornada_media_dia_s) + '/dia</div>') : '') + '</td>' +
                '<td>' + fmtDur(c.tempo_garagem_s) + '</td>' +
                '<td style="color:#059669;"><b>' + fmtDur(c.tempo_setor_s) + '</b></td>' +
                '<td style="color:#2563EB;"><b>' + fmtDur(c.tempo_trajeto_s) + '</b></td>' +
                '<td style="color:#D97706;"><b>' + fmtDur(c.tempo_aterro_s) + '</b><div style="font-size:0.67rem;color:#64748B;">' + c.qtd_viagens_aterro + 'x · ' + fmtPeso(c.peso_total_kg) + '</div></td>' +
                '<td>' + fmtDur(c.tempo_paradas_total_s) + '</td>' +
                '<td><b>' + c.pct_eficiencia_produtiva + '%</b></td>' +
                '<td>' + c.km_rodado_total + ' km<div style="font-size:0.67rem;color:#64748B;">Coleta: ' + c.km_coletado + ' km (' + c.vel_media_coleta_kmh + ' km/h)</div></td>' +
                '</tr>';

            if (exp) {
                var setCards = (c.setores_atendidos || []).map(function (sa) {
                    return '<tr>' +
                        '<td><b>' + esc(sa.setor) + '</b></td>' +
                        '<td>' + (isMes ? (sa.dias + ' dias') : (esc(sa.entrada_hhmm) + ' → ' + esc(sa.saida_hhmm))) + '</td>' +
                        '<td style="color:#059669;"><b>' + fmtDur(sa.tempo_s) + '</b></td>' +
                        '<td>' + fmtDur(sa.mov_s) + ' / ' + fmtDur(sa.parado_s) + '</td>' +
                        '<td>' + sa.passagens + '</td>' +
                        '<td>' + sa.km_coletado + ' km</td>' +
                        '</tr>';
                }).join('');

                html += '<tr class="pt-expand-row"><td colspan="12">' +
                    '<div class="pt-subgrid">' +
                    '  <div class="pt-subcard">' +
                    '    <div class="pt-subcard-title">🚚 Quebra Completa de Trajetos & Garagem</div>' +
                    '    <div class="pt-stat-list">' +
                    '      <div class="pt-stat-item"><span>Garagem Pré-Saída / Pós-Retorno</span><b>' + fmtDur(c.garagem_pre_saida_s) + ' / ' + fmtDur(c.garagem_pos_retorno_s) + '</b></div>' +
                    '      <div class="pt-stat-item"><span>Garagem → 1º Setor</span><b>' + fmtDur(c.trajeto_garagem_setor_s) + '</b></div>' +
                    '      <div class="pt-stat-item"><span>Setor → Aterro (Ida Carregado)</span><b>' + fmtDur(c.trajeto_setor_aterro_s) + '</b></div>' +
                    '      <div class="pt-stat-item"><span>Aterro → Setor (Retorno Vazio)</span><b>' + fmtDur(c.trajeto_aterro_setor_s) + '</b></div>' +
                    '      <div class="pt-stat-item"><span>Deslocamento Entre Setores</span><b>' + fmtDur(c.trajeto_entre_setores_s) + '</b></div>' +
                    '      <div class="pt-stat-item"><span>Retorno Final → Garagem</span><b>' + fmtDur(c.trajeto_retorno_garagem_s) + '</b></div>' +
                    '      <div class="pt-stat-item"><span>Refeição / SOS / Parada Externa</span><b>' + fmtDur(c.tempo_refeicao_s) + ' / ' + fmtDur(c.tempo_sos_s) + ' / ' + fmtDur(c.tempo_parada_externa_s) + '</b></div>' +
                    '    </div>' +
                    '  </div>' +
                    '  <div class="pt-subcard" style="grid-column:span 2;">' +
                    '    <div class="pt-subcard-title">🗺️ Setores Executados por ' + esc(c.placa) + ' (Tempo exato em cada setor)</div>' +
                    (setCards ? ('<div class="pt-table-wrap"><table class="pt-table" style="min-width:520px;"><thead><tr><th>Setor</th><th>' + (isMes ? 'Dias' : 'Horário') + '</th><th>Tempo no Setor</th><th>Mov. / Parado</th><th>Passagens</th><th>Km Coletado</th></tr></thead><tbody>' + setCards + '</tbody></table></div>') : '<div style="color:#64748B;font-size:0.74rem;">Sem setores associados diretamente neste período.</div>') +
                    '  </div>' +
                    '</div>' +
                    '</td></tr>';
            }
            return html;
        }).join('');

        box.innerHTML = [
            '<div class="pt-panel">',
            '  <div class="pt-panel-head">',
            '    <h3>Frota & Decomposição Completa de Jornada (' + caminhoes.length + ' caminhões) — Clique para detalhar setores e trajetos</h3>',
            '    <span style="font-size:0.72rem;color:#64748B;">Eficiência Produtiva = Tempo em Setor ÷ Jornada Total</span>',
            '  </div>',
            '  <div class="pt-table-wrap"><table class="pt-table" id="ptTblCaminhoes">',
            '    <thead><tr>',
            '      <th data-col="placa">Caminhão</th>',
            '      <th data-col="turno">Turno</th>',
            '      <th data-col="motorista">Motorista</th>',
            '      <th data-col="saida_garagem_hhmm">Saída → Chegada</th>',
            '      <th data-col="jornada_total_s">Jornada</th>',
            '      <th data-col="tempo_garagem_s">Garagem</th>',
            '      <th data-col="tempo_setor_s">Em Setor</th>',
            '      <th data-col="tempo_trajeto_s">Trajeto</th>',
            '      <th data-col="tempo_aterro_s">Aterro</th>',
            '      <th data-col="tempo_paradas_total_s">Paradas/Ref</th>',
            '      <th data-col="pct_eficiencia_produtiva">% Eficiência</th>',
            '      <th data-col="km_rodado_total">Km / Veloc.</th>',
            '    </tr></thead>',
            '    <tbody>' + (rowsHtml || '<tr><td colspan="12" style="text-align:center;color:#64748B;padding:24px;">Nenhum caminhão encontrado.</td></tr>') + '</tbody>',
            '  </table></div>',
            '</div>'
        ].join('');

        box.querySelectorAll('#ptTblCaminhoes thead th').forEach(function (th) {
            th.addEventListener('click', function () {
                var col = th.getAttribute('data-col');
                if (state.sortCam.col === col) state.sortCam.dir *= -1;
                else { state.sortCam.col = col; state.sortCam.dir = -1; }
                renderAbaCaminhoes(box);
            });
        });
        box.querySelectorAll('#ptTblCaminhoes tbody tr.pt-row-main').forEach(function (tr) {
            tr.addEventListener('click', function () {
                var pl = tr.getAttribute('data-placa');
                state.expandCam[pl] = !state.expandCam[pl];
                renderAbaCaminhoes(box);
            });
        });
    }

    function renderAbaTimeline(box) {
        var r = state.dados.resumo || {};
        if (r.modo === 'mes') {
            var evs = state.dados.evolucao_diaria || [];
            var maxH = Math.max(1, Math.max.apply(null, evs.map(function (e) {
                return (e.tempo_setor_s + e.tempo_trajeto_s + e.tempo_aterro_s + e.tempo_garagem_s + e.tempo_paradas_s) / 3600.0;
            })));
            var barsHtml = evs.map(function (ev) {
                var hSet = ev.tempo_setor_s / 3600.0;
                var hTra = ev.tempo_trajeto_s / 3600.0;
                var hAte = ev.tempo_aterro_s / 3600.0;
                var hGar = ev.tempo_garagem_s / 3600.0;
                var hPar = ev.tempo_paradas_s / 3600.0;
                var totH = hSet + hTra + hAte + hGar + hPar;
                var pxTot = Math.max(4, Math.round((totH / maxH) * 115));
                function pxPart(v) { return totH > 0 ? Math.max(0, Math.round((v / totH) * pxTot)) : 0; }
                return '<div class="pt-bar-col" data-dia="' + esc(ev.data_iso) + '" title="' + esc(ev.data_fmt + ' (' + ev.dia_semana + '): ' + fmtDur(totH * 3600) + ' | Cobertura ' + ev.pct_cobertura + '% — Clique para abrir Gantt do dia') + '">' +
                    '  <div class="pt-bar-stack" style="height:' + pxTot + 'px;">' +
                    '    <div style="height:' + pxPart(hSet) + 'px;background:' + COR_ESTADO.SETOR + ';"></div>' +
                    '    <div style="height:' + pxPart(hTra) + 'px;background:' + COR_ESTADO.TRAJETO + ';"></div>' +
                    '    <div style="height:' + pxPart(hAte) + 'px;background:' + COR_ESTADO.ATERRO + ';"></div>' +
                    '    <div style="height:' + pxPart(hGar) + 'px;background:' + COR_ESTADO.GARAGEM + ';"></div>' +
                    '    <div style="height:' + pxPart(hPar) + 'px;background:' + COR_ESTADO.PARADA_EXTERNA + ';"></div>' +
                    '  </div>' +
                    '  <span class="pt-bar-lbl">' + esc(ev.data_fmt.slice(0, 2)) + '</span>' +
                    '</div>';
            }).join('');

            var trsHtml = evs.map(function (ev) {
                return '<tr>' +
                    '<td><b>' + esc(ev.data_fmt) + '</b> <span style="color:#64748B;">(' + esc(ev.dia_semana) + ')</span></td>' +
                    '<td>' + ev.caminhoes_ativos + '</td>' +
                    '<td>' + ev.setores_executados + ' / ' + ev.setores_previstos + '</td>' +
                    '<td><b>' + ev.pct_cobertura + '%</b></td>' +
                    '<td style="color:#059669;"><b>' + fmtDur(ev.tempo_setor_s) + '</b></td>' +
                    '<td style="color:#2563EB;">' + fmtDur(ev.tempo_trajeto_s) + '</td>' +
                    '<td style="color:#D97706;">' + fmtDur(ev.tempo_aterro_s) + ' (' + ev.viagens_aterro + 'x)</td>' +
                    '<td>' + fmtDur(ev.tempo_garagem_s) + '</td>' +
                    '<td>' + fmtDur(ev.duracao_media_setor_s) + '</td>' +
                    '<td>' + fmtPeso(ev.peso_total_kg) + '</td>' +
                    '<td><button type="button" class="pt-quick-btn pt-btn-drill-dia" data-dia="' + esc(ev.data_iso) + '">🔍 Ver Gantt</button></td>' +
                    '</tr>';
            }).join('');

            box.innerHTML = [
                '<div class="pt-panel" style="margin-bottom:12px;">',
                '  <div class="pt-panel-head"><h3>Evolução Diária de Horas Operadas no Mês</h3><span style="font-size:0.72rem;color:#64748B;">Clique em qualquer barra ou botão para abrir o Gantt 24h do dia</span></div>',
                '  <div class="pt-bars-chart">' + barsHtml + '</div>',
                '</div>',
                '<div class="pt-panel">',
                '  <div class="pt-table-wrap"><table class="pt-table"><thead><tr><th>Dia</th><th>Caminhões</th><th>Setores</th><th>% Cob.</th><th>Em Setor</th><th>Trajeto</th><th>Aterro</th><th>Garagem</th><th>Média/Setor</th><th>Peso</th><th>Detalhe</th></tr></thead><tbody>' + trsHtml + '</tbody></table></div>',
                '</div>'
            ].join('');

            box.querySelectorAll('.pt-bar-col, .pt-btn-drill-dia').forEach(function (el) {
                el.addEventListener('click', function () {
                    var dia = el.getAttribute('data-dia');
                    if (!dia) return;
                    state.mesBackup = { mes: state.mes, dados: state.dados, jobId: state.jobId };
                    state.modo = 'dia';
                    state.data = dia;
                    state.abaAtiva = 'timeline';
                    $('ptBtnVoltarMes').style.display = 'inline-flex';
                    sincronizarControlesTopo();
                    carregarDados(false);
                });
            });
            return;
        }

        // Modo Dia: Gantt 24h
        var win = r.janela_operacional || {};
        var durWin = Math.max(3600, Number(win.duracao_s || 86400));
        var horaIni = parseInt(String(win.hora_inicial || '05:00').slice(0, 2), 10) || 5;
        var ticks = [];
        for (var t = 0; t <= 24; t += 3) {
            ticks.push(String((horaIni + t) % 24).padStart(2, '0') + ':00');
        }

        var rowsGantt = '';
        if (state.ganttModo === 'caminhao') {
            rowsGantt = filtrarCaminhoes().map(function (c) {
                var blks = (c.timeline || []).map(function (b) {
                    var left = Math.max(0, Math.min(100, (b.start_s / durWin) * 100));
                    var width = Math.max(0.4, Math.min(100 - left, (b.dur_s / durWin) * 100));
                    var cor = COR_ESTADO[b.kind] || '#94A3B8';
                    var tip = c.placa + ' · ' + (b.setor || NOME_ESTADO[b.kind] || b.kind) + ' (' + b.start_hhmm + ' → ' + b.end_hhmm + ' · ' + fmtDur(b.dur_s) + ')';
                    return '<div class="pt-gantt-blk" style="left:' + left.toFixed(2) + '%;width:' + width.toFixed(2) + '%;background:' + cor + ';" title="' + esc(tip) + '"></div>';
                }).join('');
                return '<div class="pt-gantt-row"><div class="pt-gantt-label" title="' + esc(c.placa + ' - ' + c.motorista) + '">' + esc(c.placa) + '</div><div class="pt-gantt-track">' + blks + '</div><div class="pt-gantt-summary">' + fmtDur(c.tempo_setor_s) + ' / ' + fmtDur(c.jornada_total_s) + '</div></div>';
            }).join('');
        } else {
            rowsGantt = filtrarSetores().filter(function (s) { return s.timeline && s.timeline.length > 0; }).map(function (s) {
                var blks = (s.timeline || []).map(function (b) {
                    var left = Math.max(0, Math.min(100, (b.start_s / durWin) * 100));
                    var width = Math.max(0.4, Math.min(100 - left, (b.dur_s / durWin) * 100));
                    var tip = s.codigo + ' · Caminhão ' + b.placa + ' (' + b.start_hhmm + ' → ' + b.end_hhmm + ' · ' + fmtDur(b.dur_s) + ')';
                    return '<div class="pt-gantt-blk" style="left:' + left.toFixed(2) + '%;width:' + width.toFixed(2) + '%;background:' + COR_ESTADO.SETOR + ';" title="' + esc(tip) + '"></div>';
                }).join('');
                return '<div class="pt-gantt-row"><div class="pt-gantt-label" title="' + esc(s.codigo) + '">' + esc(s.codigo) + '</div><div class="pt-gantt-track">' + blks + '</div><div class="pt-gantt-summary">' + fmtDur(s.tempo_efetivo_s) + ' (' + s.qtd_caminhoes + 'x)</div></div>';
            }).join('');
        }

        box.innerHTML = [
            '<div class="pt-panel">',
            '  <div class="pt-panel-head">',
            '    <h3>Linha do Tempo Operacional 24h (' + esc(win.hora_inicial || '05:00') + ' → ' + esc(win.hora_final || '05:00') + ' +1d)</h3>',
            '    <div class="pt-segmented">',
            '      <button type="button" class="pt-seg-btn ' + (state.ganttModo === 'caminhao' ? 'active' : '') + '" id="ptGanttCamBtn">🚛 Por Caminhão</button>',
            '      <button type="button" class="pt-seg-btn ' + (state.ganttModo === 'setor' ? 'active' : '') + '" id="ptGanttSetBtn">🗺️ Por Setor</button>',
            '    </div>',
            '  </div>',
            '  <div class="pt-comp-legend" style="margin-top:0;margin-bottom:10px;">',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.SETOR + ';"></span>Em Setor</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.TRAJETO + ';"></span>Trajeto</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.ATERRO + ';"></span>Aterro</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.GARAGEM + ';"></span>Garagem</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.REFEICAO + ';"></span>Refeição</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.SOS + ';"></span>SOS / Manutenção</span>',
            '    <span><span class="pt-dot" style="background:' + COR_ESTADO.PARADA_EXTERNA + ';"></span>Parada Externa >5m</span>',
            '  </div>',
            '  <div class="pt-gantt-wrap">',
            '    <div class="pt-gantt-ruler"><div class="pt-gantt-ruler-ticks">' + ticks.map(function (tk) { return '<span>' + tk + '</span>'; }).join('') + '</div><div style="width:104px;"></div></div>',
            (rowsGantt || '<div style="padding:20px;text-align:center;color:#64748B;">Sem eventos na linha do tempo para os filtros aplicados.</div>'),
            '  </div>',
            '</div>'
        ].join('');

        $('ptGanttCamBtn').addEventListener('click', function () { state.ganttModo = 'caminhao'; renderAbaTimeline(box); });
        $('ptGanttSetBtn').addEventListener('click', function () { state.ganttModo = 'setor'; renderAbaTimeline(box); });
    }

    function renderAbaCiclo(box) {
        var r = state.dados.resumo || {};
        var viagens = filtrarViagensAterro();
        var trsViagens = viagens.map(function (v) {
            return '<tr>' +
                '<td>' + esc(v.data_fmt || r.data_formatada || '') + '</td>' +
                '<td><b>' + esc(v.placa) + '</b></td>' +
                '<td>' + esc(v.motorista || '-') + '</td>' +
                '<td>' + esc(v.setor_origem || '-') + '</td>' +
                '<td style="color:#2563EB;">' + fmtDurMin(v.trajeto_ida_s) + '</td>' +
                '<td>' + esc(v.chegada_hhmm) + ' → ' + esc(v.saida_hhmm) + '</td>' +
                '<td style="color:#D97706;"><b>' + fmtDurMin(v.duracao_s) + '</b></td>' +
                '<td style="color:#2563EB;">' + fmtDurMin(v.trajeto_volta_s) + '</td>' +
                '<td>' + esc(v.destino_pos || '-') + '</td>' +
                '<td>' + esc(v.ticket || '-') + '</td>' +
                '<td><b>' + fmtPeso(v.peso_kg) + '</b></td>' +
                '</tr>';
        }).join('');

        box.innerHTML = [
            '<div class="pt-cycle-grid" style="margin-bottom:12px;">',
            '  <div class="pt-panel">',
            '    <div class="pt-panel-head"><h3>🏭 Operação na Garagem</h3></div>',
            '    <div class="pt-stat-list">',
            '      <div class="pt-stat-item"><span>Tempo Total na Garagem</span><b>' + fmtDur(r.tempo_garagem_total_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Média por Caminhão / Dia</span><b>' + fmtDur(r.tempo_garagem_medio_caminhao_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Pré-Saída (Preparação)</span><b>' + fmtDur(r.garagem_pre_saida_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Pós-Retorno (Encerramento)</span><b>' + fmtDur(r.garagem_pos_retorno_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Paradas Intermediárias</span><b>' + fmtDur(r.garagem_intermediaria_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Saída Média Diurno / Noturno</span><b>' + esc(r.hora_media_saida_diurno || '--:--') + ' / ' + esc(r.hora_media_saida_noturno || '--:--') + '</b></div>',
            '    </div>',
            '  </div>',
            '  <div class="pt-panel">',
            '    <div class="pt-panel-head"><h3>🛣️ Decomposição dos Trajetos</h3></div>',
            '    <div class="pt-stat-list">',
            '      <div class="pt-stat-item"><span>Total em Deslocamento</span><b style="color:#2563EB;">' + fmtDur(r.tempo_trajeto_total_s) + ' (' + r.pct_tempo_trajeto + '%)</b></div>',
            '      <div class="pt-stat-item"><span>Garagem → 1º Setor</span><b>' + fmtDur(r.trajeto_garagem_setor_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Setor → Aterro (Ida Carregado)</span><b>' + fmtDur(r.trajeto_setor_aterro_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Aterro → Setor (Retorno Vazio)</span><b>' + fmtDur(r.trajeto_aterro_setor_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Transição Entre Setores</span><b>' + fmtDur(r.trajeto_entre_setores_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Retorno Final → Garagem</span><b>' + fmtDur(r.trajeto_retorno_garagem_s) + '</b></div>',
            '    </div>',
            '  </div>',
            '  <div class="pt-panel">',
            '    <div class="pt-panel-head"><h3>⚖️ Aterro, Balança & Paradas</h3></div>',
            '    <div class="pt-stat-list">',
            '      <div class="pt-stat-item"><span>Tempo Total no Aterro</span><b style="color:#D97706;">' + fmtDur(r.tempo_aterro_total_s) + ' (' + r.pct_tempo_aterro + '%)</b></div>',
            '      <div class="pt-stat-item"><span>Viagens / Descargas Registradas</span><b>' + r.qtd_viagens_aterro + ' viagens</b></div>',
            '      <div class="pt-stat-item"><span>Tempo Médio por Descarga</span><b>' + fmtDurMin(r.tempo_medio_por_descarga_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Peso Total Descarregado</span><b>' + fmtPeso(r.peso_total_kg) + '</b></div>',
            '      <div class="pt-stat-item"><span>Refeição & SOS / Manutenção</span><b>' + fmtDur(r.tempo_refeicao_total_s) + ' / ' + fmtDur(r.tempo_sos_total_s) + '</b></div>',
            '      <div class="pt-stat-item"><span>Paradas Externas (>5 min)</span><b>' + fmtDur(r.tempo_parada_externa_total_s) + '</b></div>',
            '    </div>',
            '  </div>',
            '</div>',
            '<div class="pt-panel">',
            '  <div class="pt-panel-head"><h3>Registro Detalhado de Viagens ao Aterro (' + viagens.length + ')</h3><span style="font-size:0.72rem;color:#64748B;">Ida ao aterro, permanência em descarga/fila e retorno</span></div>',
            '  <div class="pt-table-wrap"><table class="pt-table"><thead><tr><th>Data</th><th>Placa</th><th>Motorista</th><th>Origem</th><th>Trajeto Ida</th><th>Chegada → Saída</th><th>Tempo Aterro</th><th>Trajeto Volta</th><th>Destino Pós</th><th>Ticket</th><th>Peso</th></tr></thead><tbody>' + (trsViagens || '<tr><td colspan="11" style="text-align:center;color:#64748B;padding:20px;">Nenhuma descarga no aterro registrada para os filtros selecionados.</td></tr>') + '</tbody></table></div>',
            '</div>'
        ].join('');
    }

    function renderAbaInsights(box) {
        var ah = state.dados.atividade_horaria || [];
        var destaques = state.dados.destaques || [];
        var maxMin = Math.max(1, Math.max.apply(null, ah.map(function (x) {
            return x.em_setor_min + x.em_trajeto_min + x.em_aterro_min + x.em_garagem_min + x.em_parada_min;
        })));

        var hourlyBars = ah.slice(0, 24).map(function (h) {
            var tot = h.em_setor_min + h.em_trajeto_min + h.em_aterro_min + h.em_garagem_min + h.em_parada_min;
            var pxTot = Math.max(3, Math.round((tot / maxMin) * 115));
            function pxP(v) { return tot > 0 ? Math.max(0, Math.round((v / tot) * pxTot)) : 0; }
            return '<div class="pt-bar-col" title="' + esc(h.hora_rotulo + ': Setor ' + Math.round(h.em_setor_min) + 'm | Trajeto ' + Math.round(h.em_trajeto_min) + 'm | Aterro ' + Math.round(h.em_aterro_min) + 'm | Garagem ' + Math.round(h.em_garagem_min) + 'm') + '">' +
                '  <div class="pt-bar-stack" style="height:' + pxTot + 'px;">' +
                '    <div style="height:' + pxP(h.em_setor_min) + 'px;background:' + COR_ESTADO.SETOR + ';"></div>' +
                '    <div style="height:' + pxP(h.em_trajeto_min) + 'px;background:' + COR_ESTADO.TRAJETO + ';"></div>' +
                '    <div style="height:' + pxP(h.em_aterro_min) + 'px;background:' + COR_ESTADO.ATERRO + ';"></div>' +
                '    <div style="height:' + pxP(h.em_garagem_min) + 'px;background:' + COR_ESTADO.GARAGEM + ';"></div>' +
                '    <div style="height:' + pxP(h.em_parada_min) + 'px;background:' + COR_ESTADO.PARADA_EXTERNA + ';"></div>' +
                '  </div>' +
                '  <span class="pt-bar-lbl">' + esc(h.hora_rotulo.slice(0, 2)) + 'h</span>' +
                '</div>';
        }).join('');

        var insHtml = destaques.map(function (ins) {
            return '<div class="pt-insight-card"><h4>' + esc(ins.titulo) + '</h4><p>' + esc(ins.texto) + '</p></div>';
        }).join('');

        box.innerHTML = [
            '<div class="pt-panel" style="margin-bottom:12px;">',
            '  <div class="pt-panel-head"><h3>Curva de Ocupação Horária da Frota (Minutos acumulados por hora da janela operacional)</h3></div>',
            '  <div class="pt-bars-chart">' + hourlyBars + '</div>',
            '</div>',
            '<div class="pt-panel">',
            '  <div class="pt-panel-head"><h3>Diagnóstico Operacional Automático & Destaques</h3></div>',
            '  <div class="pt-insights-grid">' + (insHtml || '<div style="color:#64748B;font-size:0.76rem;">Sem destaques suficientes no período selecionado.</div>') + '</div>',
            '</div>'
        ].join('');
    }

    function init() {
        var btn = $('btnAbrirPainelTempos');
        if (btn) {
            btn.addEventListener('click', abrirPainel);
        }
        window.painelTemposLC = {
            abrir: abrirPainel,
            fechar: fecharPainel
        };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
