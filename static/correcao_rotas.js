/* ==========================================================================
   SISTEMA INTEGRADO DE CORREÇÃO DE ROTAS + CENTRAL ADMIN DE ANÁLISE DE PDFS
   Adaptado integralmente de Correcao_de_Rotas V4.html e conectado ao Sistema LC
   ========================================================================== */
(function () {
    'use strict';

    var LOGO_URL = '/static/logo-locar.png';
    var FUNDOS = [
        function (z, x, y) { return 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/' + z + '/' + y + '/' + x; },
        function (z, x, y) { return 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/' + z + '/' + y + '/' + x; }
    ];
    var FUNDO_ZOOM_MAX = 18;
    var fundoInicio = 0, fundoFalhas = [0, 0];

    var MALHA_TILEJSON = 'https://tiles.openfreemap.org/planet';
    var MALHA_Z = 14;
    var CLASSES_VIA = {
        motorway: 1, trunk: 1, primary: 1, secondary: 1, tertiary: 1,
        minor: 1, service: 1, track: 1, path: 1, pedestrian: 1,
        living_street: 1, raceway: 0, busway: 1
    };

    var ACOES = {
        manual:    { nome: 'Coleta manual',          cor: '#1f6fd1', ic: '🔧', desc: 'O caminhão não entra; a coleta é feita a pé.' },
        trajeto:   { nome: 'Somente trajeto',        cor: '#e67e22', ic: '🚚', desc: 'O caminhão só passa; o trecho deixa de ser coleta.' },
        mover:     { nome: 'Mudar de setor',         cor: '#8e44ad', ic: '↔️', desc: 'O trecho sai deste setor e vai para outro.' },
        temp:      { nome: 'Interdição temporária',  cor: '#4a5a6a', ic: '⛔', desc: 'Rua fechada por um período (obra, evento, carnaval).' },
        normal:    { nome: 'Voltar a coleta normal', cor: '#27ae60', ic: '✔️', desc: 'O trecho volta a ser coletado pelo caminhão.' },
        adicionar: { nome: 'Adicionar ao setor',     cor: '#e6007e', ic: '➕', desc: 'Rua nova incluída na rota do setor.' }
    };

    var COR_CLASS = { coleta: '#2ecc71', manual: '#3498db' };
    var LEG_EXEC_MES = [
        ['#00E600', '80–100%'], ['#FFFF00', '60–80%'], ['#FF9900', '40–60%'],
        ['#FF0000', '20–40%'], ['#660000', '0–20%'], ['#0284C7', 'Coleta manual']
    ];
    var LEG_EXEC_DIA = [
        ['#00A859', 'Coletada'], ['#E53935', 'Não coletada (Omissão)'],
        ['#0284C7', 'Coleta manual'], ['#FBC02D', 'Possível falha GPS']
    ];
    var PALETA_SETORES = [
        '#038242', '#1f6fd1', '#8e44ad', '#d35400', '#16a085',
        '#c0392b', '#2980b9', '#27ae60', '#d63031', '#6c5ce7',
        '#00838f', '#ad1457', '#558b2f', '#ef6c00', '#4527a0'
    ];

    var FILTROS_RUAS = {
        mes: [['todas', 'Todas'], ['baixa', 'Abaixo de 60%'], ['nunca', 'Nunca coletadas'], ['possivel', 'Possível manual']],
        dia: [['todas', 'Todas'], ['naocol', 'Não coletadas'], ['possivel', 'Possível manual']]
    };
    var TEXTO_STATUS = {
        collected: 'coletada',
        not_collected: 'não coletada',
        possible_gps_gap: 'possível falha de GPS',
        possible_manual: 'possível coleta manual'
    };

    // Estado da Correção de Rotas
    var correcaoAtiva = false;
    var mapHooksInstalados = false;
    var D = null;
    var pedidos = [];
    var selecao = {};
    var selMalha = {};
    var modo = 'clique';
    var turnoFiltro = 'todos';
    var setorFoco = null;
    var filtroRuas = 'todas';

    var camadaSel = null, camadaPed = null, camadaFoco = null, camadaMalha = null, camadaLaco = null, camadaAdminPreview = null;
    var rendSeg = null, rendTopo = null, rendMalha = null;
    var gruposSetor = {};
    var indice = null, indiceMalha = null;
    var malha = { arestas: {}, blocos: {}, pendentes: 0, url: null, falhou: false };
    var tooltipSeg = null;
    var lacoAtivo = false, lacoPts = [], lacoLinha = null;

    // Utilitários
    function $(id) { return document.getElementById(id); }
    function esc(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }
    function fmtM(m) {
        return m >= 1000 ? (m / 1000).toFixed(2).replace('.', ',') + ' km' : Math.round(m) + ' m';
    }
    function fmtData(iso) {
        if (!iso) return '';
        var p = String(iso).slice(0, 10).split('-');
        return p.length === 3 ? (p[2] + '/' + p[1] + '/' + p[0]) : iso;
    }
    function hojeISO() {
        var d = new Date();
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }
    function uid() {
        return 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    }
    function crToast(msg) {
        if (window.__SISTEMA_LC__ && window.__SISTEMA_LC__.mostrarToast) {
            window.__SISTEMA_LC__.mostrarToast(msg);
        }
    }
    function avisoMapa(t) {
        var e = $('crAvisoMapa');
        if (!e) return;
        if (!t) { e.style.display = 'none'; return; }
        e.textContent = t;
        e.style.display = 'block';
    }
    function distM(a, b) {
        var R = 6371000, r = Math.PI / 180, dl = (b[0] - a[0]) * r, dn = (b[1] - a[1]) * r;
        var h = Math.sin(dl / 2) * Math.sin(dl / 2) + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dn / 2) * Math.sin(dn / 2);
        return 2 * R * Math.asin(Math.sqrt(h));
    }
    function compLinha(ll) {
        var s = 0;
        for (var i = 1; i < ll.length; i++) s += distM(ll[i - 1], ll[i]);
        return s;
    }
    function meioLinha(ll) {
        var tot = compLinha(ll), alvo = tot / 2, acc = 0;
        for (var i = 1; i < ll.length; i++) {
            var d = distM(ll[i - 1], ll[i]);
            if (acc + d >= alvo && d > 0) {
                var t = (alvo - acc) / d;
                return [ll[i - 1][0] + (ll[i][0] - ll[i - 1][0]) * t, ll[i - 1][1] + (ll[i][1] - ll[i - 1][1]) * t];
            }
            acc += d;
        }
        return ll[0];
    }
    function dentroPoligono(pt, pol) {
        var x = pt[1], y = pt[0], d = false;
        for (var i = 0, j = pol.length - 1; i < pol.length; j = i++) {
            var xi = pol[i][1], yi = pol[i][0], xj = pol[j][1], yj = pol[j][0];
            if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) d = !d;
        }
        return d;
    }
    function nomesRuas(lista) {
        var c = {}, ordem = [];
        lista.forEach(function (n) {
            n = n || 'Rua sem nome';
            if (!c[n]) { c[n] = 0; ordem.push(n); }
            c[n]++;
        });
        return ordem;
    }
    function resumoRuas(nomes, max) {
        max = max || 3;
        if (nomes.length <= max) return nomes.join(', ');
        return nomes.slice(0, max).join(', ') + ' e mais ' + (nomes.length - max);
    }
    function getMap() {
        return window.__SISTEMA_LC__ ? window.__SISTEMA_LC__.getMap() : null;
    }

    // Fecho convexo (Monotone Chain) para desenhar contorno suave do setor no mapa e no PDF
    function calcularContornoSetor(fids, segsMap) {
        var pts = [];
        fids.forEach(function (fid) {
            var s = segsMap[fid];
            if (!s) return;
            pts.push([s.ll[0][1], s.ll[0][0]]);
            pts.push([s.ll[s.ll.length - 1][1], s.ll[s.ll.length - 1][0]]);
            pts.push([s.meio[1], s.meio[0]]);
        });
        if (pts.length < 3) return [];
        pts.sort(function (a, b) { return a[0] === b[0] ? a[1] - b[1] : a[0] - b[0]; });
        function cross(o, a, b) { return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]); }
        var lower = [];
        for (var i = 0; i < pts.length; i++) {
            while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], pts[i]) <= 0) lower.pop();
            lower.push(pts[i]);
        }
        var upper = [];
        for (var j = pts.length - 1; j >= 0; j--) {
            while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], pts[j]) <= 0) upper.pop();
            upper.push(pts[j]);
        }
        upper.pop();
        lower.pop();
        var hull = lower.concat(upper);
        if (hull.length < 3) return [];
        hull.push(hull[0]);
        return [{ type: 'Polygon', coordinates: [hull] }];
    }

    function turnoPorCodigo(code) {
        var s = String(code || '').toUpperCase().trim();
        if (!s) return 'Diurno';
        if (/\b(SCDOMN|SCDN|SCPN|SCIN|SCDM|SCPM|SCIM)\b/.test(s)) return 'Noturno';
        if (s.indexOf('NOTURNO') >= 0 || s.indexOf('NOITE') >= 0) return 'Noturno';
        var tok = s.split(/\s+/)[0];
        if (tok !== 'SCDOM') {
            var l = tok.charAt(tok.length - 1);
            if (l === 'N' || l === 'M') return 'Noturno';
        }
        return 'Diurno';
    }

    function corExecucaoFeature(p, ehPeriodo) {
        var ehMan = (p.status === 'possible_manual') || (Boolean(p.is_manual) && p.status !== 'not_collected');
        if (ehPeriodo) {
            var pctVal = Number(p.pct_periodo != null ? p.pct_periodo : 0);
            if (ehMan) return '#0284C7';
            if (pctVal >= 80) return '#00E600';
            if (pctVal >= 60) return '#FFFF00';
            if (pctVal >= 40) return '#FF9900';
            if (pctVal >= 20) return '#FF0000';
            return '#660000';
        }
        if (p.status === 'not_collected') return '#E53935';
        if (ehMan) return '#0284C7';
        if (p.status === 'possible_gps_gap') return '#FBC02D';
        return '#00A859';
    }

    // ======================================================================
    // CONSTRUÇÃO DE DADOS DIRETO DO MAPA ATUAL (SEM UPLOAD DE HTML)
    // ======================================================================
    function construirDadosDoMapaAtual() {
        var lc = window.__SISTEMA_LC__;
        if (!lc) return false;
        var audit = lc.getAudit();
        var feats = lc.getFeatures() || [];
        var setoresArr = lc.getSetores() || [];
        if (!audit || !audit.resumo) return false;

        var r = audit.resumo;
        var ehPeriodo = Boolean(r.periodo && r.periodo !== 'diario');
        var contratoId = String(r.contrato_id || 'cabo').toLowerCase().trim();
        var contratoNome = String(r.contrato_nome || contratoId.toUpperCase());
        var deFmt = r.periodo_inicio ? fmtData(r.periodo_inicio) : (r.data_formatada || fmtData(r.data_iso));
        var ateFmt = r.periodo_fim ? fmtData(r.periodo_fim) : deFmt;
        var tituloMapa = 'Mapa de Execução de ' + contratoNome + ' (' + (r.data_formatada || deFmt) + ')';

        D = {
            titulo: tituloMapa,
            arquivo: tituloMapa,
            contratoId: contratoId,
            contrato: contratoNome,
            de: deFmt,
            ate: ateFmt,
            dataIso: r.data_iso || '',
            periodoRaw: r.periodo || 'diario',
            periodo: ehPeriodo ? 'mes' : 'dia',
            legenda: ehPeriodo ? LEG_EXEC_MES : LEG_EXEC_DIA,
            setoresConhecidos: [],
            setoresConhecidosMeta: {},
            tiles: {},
            segs: {},
            lista: [],
            setores: {},
            codigos: [],
            ruas: [],
            turnoDeArquivo: true
        };

        setoresArr.forEach(function (stIn, idx) {
            var c = String(stIn.codigo || '').trim();
            if (!c) return;
            var tn = stIn.turno === 'NOTURNO' ? 'Noturno' : (stIn.turno === 'DIURNO' ? 'Diurno' : turnoPorCodigo(c));
            D.setores[c] = {
                code: c,
                nome: c,
                agenda: stIn.agenda_rotulo || '',
                cor: PALETA_SETORES[idx % PALETA_SETORES.length],
                turno: tn,
                geoms: [],
                fids: [],
                km: 0,
                rota: c
            };
            D.codigos.push(c);
        });

        var ruasCount = {};
        var seqFid = 1;

        feats.forEach(function (f) {
            if (!f || !f.geometry || !f.geometry.coordinates) return;
            var p = f.properties || {};
            var g = f.geometry;
            var linhas = g.type === 'MultiLineString' ? g.coordinates : [g.coordinates];
            var ll = [];
            linhas.forEach(function (l) {
                l.forEach(function (coord) {
                    if (Array.isArray(coord) && coord.length >= 2) ll.push([coord[1], coord[0]]);
                });
            });
            if (ll.length < 2) return;

            var setoresSeg = (Array.isArray(p.sectors) && p.sectors.length)
                ? p.sectors.filter(Boolean)
                : [p.sector || 'S/S'];

            var lenSeg = Number(p.len_m || 0) || compLinha(ll);
            var meioPt = meioLinha(ll);
            var corSeg = corExecucaoFeature(p, ehPeriodo);
            var pctVal = ehPeriodo
                ? Number(p.pct_periodo != null ? p.pct_periodo : 0)
                : ((p.status === 'collected' || p.status === 'possible_manual') ? 100 : 0);
            var nuncaFlag = ehPeriodo ? (Number(p.dias_coletados || 0) === 0) : (p.status === 'not_collected');
            var possManualFlag = (p.status === 'possible_manual') || Boolean(p.is_manual);

            setoresSeg.forEach(function (rawCode) {
                var code = String(rawCode || 'S/S').trim();
                if (!D.setores[code]) {
                    var idxCor = D.codigos.length;
                    D.setores[code] = {
                        code: code,
                        nome: code,
                        agenda: p.agenda_rotulo || '',
                        cor: PALETA_SETORES[idxCor % PALETA_SETORES.length],
                        turno: turnoPorCodigo(code),
                        geoms: [],
                        fids: [],
                        km: 0,
                        rota: code
                    };
                    D.codigos.push(code);
                }
                var fidVirtual = seqFid++;
                var s = {
                    fid: fidVirtual,
                    origId: p.id || p.fid || fidVirtual,
                    code: code,
                    rota: code,
                    agenda: D.setores[code].agenda,
                    rua: p.street_name || '',
                    cls: p.is_manual ? 'manual' : 'coleta',
                    pct: pctVal,
                    cor: corSeg,
                    len: lenSeg,
                    ll: ll,
                    meio: meioPt,
                    possManual: possManualFlag,
                    nunca: nuncaFlag,
                    shared: setoresSeg.slice(),
                    status: p.status || null
                };
                D.segs[fidVirtual] = s;
                D.lista.push(s);
                D.setores[code].fids.push(fidVirtual);
                D.setores[code].km += lenSeg / 1000;
                if (s.rua) ruasCount[s.rua] = (ruasCount[s.rua] || 0) + 1;
            });
        });

        D.codigos = D.codigos.filter(function (c) { return D.setores[c].fids.length > 0; });
        D.codigos.sort(function (a, b) { return a.localeCompare(b, 'pt-BR', { numeric: true }); });
        D.codigos.forEach(function (c) {
            D.setores[c].geoms = calcularContornoSetor(D.setores[c].fids, D.segs);
        });
        D.ruas = Object.keys(ruasCount).sort(function (a, b) { return a.localeCompare(b, 'pt-BR'); });

        // Busca todos os setores do contrato (inclusive de outros dias da semana) para "Mudar de setor"
        carregarSetoresCompletosContrato(contratoId);
        return true;
    }

    function carregarSetoresCompletosContrato(contratoId) {
        if (!contratoId || contratoId === 'all') return;
        var sh = window.__SISTEMA_LC__ ? window.__SISTEMA_LC__.getSharedInfo() : {};
        var qs = 'cidade=' + encodeURIComponent(contratoId);
        if (sh.snapshotId) qs += '&snapshot_id=' + encodeURIComponent(sh.snapshotId);
        if (sh.token) qs += '&token=' + encodeURIComponent(sh.token);
        fetch('/api/correcao-rotas/setores-contrato?' + qs)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (j) {
                if (!j || !j.sucesso || !D || D.contratoId !== contratoId) return;
                var lista = j.setores || [];
                D.setoresConhecidos = lista.map(function (x) { return x.codigo; });
                lista.forEach(function (x) { D.setoresConhecidosMeta[x.codigo] = x; });
            })
            .catch(function () {});
    }

    // ======================================================================
    // CAMADAS E INTERAÇÃO NO MAPA PRINCIPAL DO SISTEMA
    // ======================================================================
    function garantirCamadasNoMapa() {
        var m = getMap();
        if (!m || mapHooksInstalados) return;
        mapHooksInstalados = true;

        if (!m.getPane('crFoco')) {
            m.createPane('crFoco');
            m.getPane('crFoco').style.zIndex = 395;
            m.getPane('crFoco').style.pointerEvents = 'none';
        }
        if (!m.getPane('crMalha')) {
            m.createPane('crMalha');
            m.getPane('crMalha').style.zIndex = 405;
        }
        if (!m.getPane('crSegs')) {
            m.createPane('crSegs');
            m.getPane('crSegs').style.zIndex = 415;
        }
        if (!m.getPane('crTopo')) {
            m.createPane('crTopo');
            m.getPane('crTopo').style.zIndex = 445;
        }

        rendSeg = L.canvas({ pane: 'crSegs', padding: 0.3 });
        rendTopo = L.canvas({ pane: 'crTopo', padding: 0.3 });
        rendMalha = L.canvas({ pane: 'crMalha', padding: 0.3 });

        camadaFoco = L.layerGroup();
        camadaMalha = L.layerGroup();
        camadaPed = L.layerGroup();
        camadaSel = L.layerGroup();
        camadaLaco = L.layerGroup();
        camadaAdminPreview = L.layerGroup();
        tooltipSeg = L.tooltip({ className: 'cr-dica-seg', direction: 'top', offset: [0, -8] });

        m.on('click', aoClicarMapa);
        m.on('mousemove', aoMoverMapa);
        m.on('mouseout', function () { if (correcaoAtiva) m.closeTooltip(tooltipSeg); });
        m.on('moveend', function () { if (correcaoAtiva && modo === 'add') agendarMalha(); });
        configurarLaco(m);
    }

    function limparCamadasCorrecao() {
        var m = getMap();
        if (!m) return;
        for (var c in gruposSetor) {
            if (m.hasLayer(gruposSetor[c])) m.removeLayer(gruposSetor[c]);
        }
        gruposSetor = {};
        [camadaFoco, camadaMalha, camadaPed, camadaSel, camadaLaco].forEach(function (ly) {
            if (ly) {
                ly.clearLayers();
                if (m.hasLayer(ly)) m.removeLayer(ly);
            }
        });
        if (tooltipSeg && m.hasLayer(tooltipSeg)) m.closeTooltip(tooltipSeg);
        m.dragging.enable();
        m.getContainer().style.cursor = '';
        avisoMapa(null);
    }

    function desenharSegmentos() {
        var m = getMap();
        if (!m || !D) return;
        for (var c in gruposSetor) {
            if (m.hasLayer(gruposSetor[c])) m.removeLayer(gruposSetor[c]);
        }
        gruposSetor = {};
        camadaFoco.addTo(m);
        camadaPed.addTo(m);
        camadaSel.addTo(m);
        camadaLaco.addTo(m);

        D.codigos.forEach(function (c) {
            var g = L.layerGroup();
            D.setores[c].fids.forEach(function (fid) {
                var s = D.segs[fid];
                s.layer = L.polyline(s.ll, {
                    renderer: rendSeg,
                    color: s.cor || '#999',
                    weight: 3.2,
                    opacity: 0.92,
                    interactive: false
                });
                g.addLayer(s.layer);
            });
            gruposSetor[c] = g;
        });
        montarIndice();
        aplicarFiltrosCorrecao(false);
    }

    function setorVisivel(c) {
        return turnoFiltro === 'todos' || (D.setores[c] && D.setores[c].turno === turnoFiltro);
    }
    function passaFiltroRuas(s) {
        if (filtroRuas === 'naocol') return s.status === 'not_collected' || s.status === 'manual_not_collected';
        if (filtroRuas === 'baixa') return s.cls !== 'manual' && s.pct != null && s.pct < 60;
        if (filtroRuas === 'nunca') return s.nunca;
        if (filtroRuas === 'possivel') return s.possManual;
        return true;
    }
    function segAtivo(s) {
        return s && setorVisivel(s.code) && (!setorFoco || s.code === setorFoco) && passaFiltroRuas(s);
    }
    function segVisivelSemFoco(s) {
        return s && setorVisivel(s.code) && passaFiltroRuas(s);
    }

    function aplicarFiltrosCorrecao(enquadrar) {
        var m = getMap();
        if (!m || !D) return;
        D.codigos.forEach(function (c) {
            var g = gruposSetor[c], vis = setorVisivel(c);
            if (!g) return;
            if (vis && !m.hasLayer(g)) g.addTo(m);
            if (!vis && m.hasLayer(g)) m.removeLayer(g);
            if (vis) {
                var apagado = setorFoco && c !== setorFoco;
                D.setores[c].fids.forEach(function (fid) {
                    var s = D.segs[fid];
                    s.layer.setStyle({
                        opacity: !passaFiltroRuas(s) ? 0 : (apagado ? 0.18 : 0.92),
                        weight: apagado ? 2 : 3.2
                    });
                });
            }
        });
        if (setorFoco && gruposSetor[setorFoco] && m.hasLayer(gruposSetor[setorFoco])) {
            gruposSetor[setorFoco].eachLayer(function (l) { l.bringToFront(); });
        }
        camadaFoco.clearLayers();
        if (setorFoco && D.setores[setorFoco]) {
            var st = D.setores[setorFoco];
            st.geoms.forEach(function (g) {
                L.geoJSON(g, {
                    pane: 'crFoco',
                    style: { color: st.cor, weight: 2, opacity: 0.9, fillColor: st.cor, fillOpacity: 0.08, interactive: false }
                }).addTo(camadaFoco);
            });
        }
        Object.keys(selecao).forEach(function (fid) {
            if (!segAtivo(D.segs[fid])) delete selecao[fid];
        });
        desenharSelecao();
        desenharPedidosMapa();
        renderListaSetores();
        desenharLegenda();
        atualizarDicaModo();
        if (enquadrar) enquadrarAtivos();
    }

    function enquadrarAtivos() {
        var m = getMap();
        if (!m || !D) return;
        var b = null;
        D.lista.forEach(function (s) {
            if (!segAtivo(s)) return;
            s.ll.forEach(function (p) {
                if (!b) b = L.latLngBounds(p, p);
                else b.extend(p);
            });
        });
        if (b) m.fitBounds(b, { padding: [30, 30] });
    }

    // ======================================================================
    // ÍNDICE ESPACIAL E CLIQUE PRECISO
    // ======================================================================
    var CEL = 0.0025;
    function indexar(idx, id, ll) {
        var a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
        ll.forEach(function (p) {
            if (p[0] < a) a = p[0]; if (p[0] > c) c = p[0];
            if (p[1] < b) b = p[1]; if (p[1] > d) d = p[1];
        });
        for (var i = Math.floor(a / CEL); i <= Math.floor(c / CEL); i++) {
            for (var j = Math.floor(b / CEL); j <= Math.floor(d / CEL); j++) {
                var k = i + ':' + j;
                (idx[k] || (idx[k] = [])).push(id);
            }
        }
    }
    function montarIndice() {
        indice = {};
        D.lista.forEach(function (s) { indexar(indice, s.fid, s.ll); });
    }
    function candidatos(idx, latlng, tolPx) {
        var m = getMap();
        if (!m || !idx) return [];
        var p0 = m.latLngToContainerPoint(latlng);
        var ll2 = m.containerPointToLatLng([p0.x + tolPx, p0.y + tolPx]);
        var dLat = Math.abs(ll2.lat - latlng.lat), dLng = Math.abs(ll2.lng - latlng.lng), out = {};
        for (var i = Math.floor((latlng.lat - dLat) / CEL); i <= Math.floor((latlng.lat + dLat) / CEL); i++) {
            for (var j = Math.floor((latlng.lng - dLng) / CEL); j <= Math.floor((latlng.lng + dLng) / CEL); j++) {
                (idx[i + ':' + j] || []).forEach(function (id) { out[id] = 1; });
            }
        }
        return Object.keys(out);
    }
    function distPx(p, ll) {
        var m = getMap();
        var best = Infinity;
        var pts = ll.map(function (c) { return m.latLngToContainerPoint(c); });
        for (var i = 1; i < pts.length; i++) {
            var a = pts[i - 1], b = pts[i], dx = b.x - a.x, dy = b.y - a.y, L2 = dx * dx + dy * dy;
            var t = L2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L2)) : 0;
            var x = a.x + t * dx - p.x, y = a.y + t * dy - p.y, d = Math.sqrt(x * x + y * y);
            if (d < best) best = d;
        }
        return best;
    }
    function maisProximo(latlng) {
        var m = getMap();
        var p = m.latLngToContainerPoint(latlng), best = null, bd = 9;
        candidatos(indice, latlng, 9).forEach(function (fid) {
            var s = D.segs[fid];
            if (!segAtivo(s)) return;
            var d = distPx(p, s.ll);
            if (d < bd) { bd = d; best = s; }
        });
        return best;
    }
    function distM_pontoLinha(pt, ll, k) {
        var best = Infinity, px = pt[1] * k, py = pt[0] * 111320;
        for (var i = 1; i < ll.length; i++) {
            var ax = ll[i - 1][1] * k, ay = ll[i - 1][0] * 111320, bx = ll[i][1] * k, by = ll[i][0] * 111320;
            var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
            var t = L2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) : 0;
            var x = ax + t * dx - px, y = ay + t * dy - py, d = Math.sqrt(x * x + y * y);
            if (d < best) best = d;
        }
        return best;
    }
    function codigosNaRota(a) {
        if (a._cods) return a._cods;
        var cods = {}, k = Math.cos(a.meio[0] * Math.PI / 180) * 111320;
        var i0 = Math.floor(a.meio[0] / CEL), j0 = Math.floor(a.meio[1] / CEL), vistos = {};
        for (var i = i0 - 1; i <= i0 + 1; i++) {
            for (var j = j0 - 1; j <= j0 + 1; j++) {
                (indice[i + ':' + j] || []).forEach(function (fid) {
                    if (vistos[fid]) return;
                    vistos[fid] = 1;
                    var s = D.segs[fid];
                    if (s && distM_pontoLinha(a.meio, s.ll, k) < 8) cods[s.code] = 1;
                });
            }
        }
        return (a._cods = Object.keys(cods));
    }
    function malhaMaisProxima(latlng) {
        if (!indiceMalha) return null;
        var m = getMap();
        var p = m.latLngToContainerPoint(latlng), best = null, bd = 9;
        candidatos(indiceMalha, latlng, 9).forEach(function (id) {
            var a = malha.arestas[id];
            if (!a) return;
            var d = distPx(p, a.ll);
            if (d < bd) { bd = d; best = a; }
        });
        return best;
    }

    function setoresNoPonto(latlng) {
        var m = getMap();
        var p = m.latLngToContainerPoint(latlng), melhor = {};
        candidatos(indice, latlng, 9).forEach(function (fid) {
            var s = D.segs[fid];
            if (!segVisivelSemFoco(s)) return;
            var d = distPx(p, s.ll);
            if (d < 9 && (!melhor[s.code] || d < melhor[s.code].d)) melhor[s.code] = { d: d, s: s };
        });
        return Object.keys(melhor).map(function (k) { return melhor[k].s; });
    }

    function escolherSetorNaDivisa(latlng, ops) {
        var m = getMap();
        var h = '<div class="cr-popup-setores"><b>' + esc(ops[0].rua || 'Esta rua') + '</b> está em ' + ops.length + ' setores.<br>Qual você quer alterar?';
        ops.forEach(function (s) {
            h += '<button data-c="' + esc(s.code) + '" data-f="' + s.fid + '">' + esc(s.code) +
                 ' <span style="font-weight:400;color:#5d6b62">· ' + esc((D.setores[s.code] && D.setores[s.code].agenda) || '') + '</span></button>';
        });
        h += '</div>';
        var pop = L.popup({ maxWidth: 265 }).setLatLng(latlng).setContent(h).openOn(m);
        setTimeout(function () {
            var el = pop.getElement();
            if (!el) return;
            Array.prototype.forEach.call(el.querySelectorAll('button'), function (b) {
                b.onclick = function () {
                    m.closePopup();
                    focarSetor(b.dataset.c, true);
                    selecao[Number(b.dataset.f)] = true;
                    desenharSelecao();
                    renderSelecao();
                };
            });
        }, 20);
    }

    function aoClicarMapa(e) {
        if (!correcaoAtiva || !D || modo === 'laco') return;
        if (modo === 'add') {
            if (!setorFoco) { crToast('Primeiro escolha, na lista à esquerda, o setor que vai receber as ruas.'); return; }
            var a = malhaMaisProxima(e.latlng);
            if (a) {
                if (!selMalha[a.id] && codigosNaRota(a).indexOf(setorFoco) >= 0) {
                    crToast('Essa rua já faz parte do setor ' + setorFoco + '.');
                    return;
                }
                if (selMalha[a.id]) delete selMalha[a.id];
                else selMalha[a.id] = a;
                desenharSelecao();
                renderSelecao();
            }
            return;
        }
        var s = maisProximo(e.latlng);
        if (!s) return;
        if (!setorFoco) {
            var ops = setoresNoPonto(e.latlng);
            if (ops.length > 1) { escolherSetorNaDivisa(e.latlng, ops); return; }
            focarSetor(s.code, true);
            selecao[s.fid] = true;
            desenharSelecao();
            renderSelecao();
            crToast('Setor em foco: ' + s.code);
            return;
        }
        if (selecao[s.fid]) delete selecao[s.fid];
        else selecao[s.fid] = true;
        desenharSelecao();
        renderSelecao();
    }

    var _ultMov = 0;
    function aoMoverMapa(e) {
        if (!correcaoAtiva || !D) return;
        var t = Date.now();
        if (t - _ultMov < 40) return;
        _ultMov = t;
        if (modo === 'laco' && lacoAtivo) return;
        var m = getMap();
        var html = null;
        if (modo === 'add') {
            var a = malhaMaisProxima(e.latlng);
            if (a) {
                var cds = codigosNaRota(a);
                html = '<b>' + esc(a.nome || 'Rua sem nome') + '</b><br>' +
                    (cds.indexOf(setorFoco) >= 0
                        ? 'Já faz parte do ' + esc(setorFoco)
                        : (cds.length ? 'Hoje está no setor ' + esc(cds.join(', ')) + '<br>' : '') +
                          (selMalha[a.id] ? 'Selecionada · clique para tirar' : 'Clique para escolher'));
            }
        } else {
            var s = maisProximo(e.latlng);
            if (s) {
                var pd = pedidoDoTrecho(s.fid);
                html = '<b>' + esc(s.rua || 'Rua sem nome') + '</b><br>' + esc(s.code) +
                    (D.periodo === 'dia'
                        ? (s.status ? ' · ' + (TEXTO_STATUS[s.status] || s.status) : '')
                        : (s.pct != null ? ' · ' + Math.round(s.pct) + '% coletado' : '')) +
                    (pd ? '<br>Pedido: ' + ACOES[pd.tipo].nome : '');
            }
        }
        if (html) {
            tooltipSeg.setLatLng(e.latlng).setContent(html);
            if (!m.hasLayer(tooltipSeg)) tooltipSeg.addTo(m);
            m.getContainer().style.cursor = 'pointer';
        } else {
            if (m.hasLayer(tooltipSeg)) m.closeTooltip(tooltipSeg);
            m.getContainer().style.cursor = modo === 'laco' ? 'crosshair' : '';
        }
    }

    // ======================================================================
    // FERRAMENTA LAÇO (CIRCULAR UMA ÁREA)
    // ======================================================================
    function configurarLaco(m) {
        var cont = m.getContainer();
        function ini(e) {
            if (!correcaoAtiva || modo !== 'laco') return;
            if (e.button !== undefined && e.button !== 0) return;
            lacoAtivo = true;
            lacoPts = [m.mouseEventToLatLng(e)];
            lacoLinha = L.polyline(lacoPts, { color: '#e00000', weight: 3, dashArray: '6,6', interactive: false }).addTo(camadaLaco);
            e.preventDefault();
        }
        function mov(e) {
            if (!lacoAtivo) return;
            lacoPts.push(m.mouseEventToLatLng(e));
            lacoLinha.setLatLngs(lacoPts);
        }
        function fim() {
            if (!lacoAtivo) return;
            lacoAtivo = false;
            var pol = lacoPts.map(function (p) { return [p.lat, p.lng]; });
            var linha = lacoLinha;
            setTimeout(function () { if (camadaLaco && linha) camadaLaco.removeLayer(linha); }, 700);
            if (pol.length < 4 || !D) return;
            var n = 0, jaNoSetor = 0;
            if (!setorFoco) {
                crToast('Escolha o setor na lista à esquerda (ou clique numa rua dele) antes de circular a área.');
                return;
            }
            D.lista.forEach(function (s) {
                if (segAtivo(s) && dentroPoligono(s.meio, pol)) {
                    selecao[s.fid] = true;
                    n++;
                }
            });
            desenharSelecao();
            renderSelecao();
            if (!n && !jaNoSetor) crToast('Nenhuma rua ativa dentro da área circulada.');
        }
        cont.addEventListener('mousedown', ini);
        cont.addEventListener('mousemove', mov);
        window.addEventListener('mouseup', fim);
    }

    // ======================================================================
    // MALHA CANDIDATA ("ADICIONAR RUAS"): OPENFREEMAP MVT + FALLBACK GPKG LOCAL
    // ======================================================================
    function Pbf(buf) { this.b = new Uint8Array(buf); this.p = 0; }
    Pbf.prototype.varint = function () {
        var r = 0, s = 0, b;
        do { b = this.b[this.p++]; r += (b & 0x7f) * Math.pow(2, s); s += 7; } while (b >= 0x80);
        return r;
    };
    Pbf.prototype.svarint = function () { var n = this.varint(); return n % 2 === 1 ? (n + 1) / -2 : n / 2; };
    Pbf.prototype.str = function (n) { var s = new TextDecoder().decode(this.b.subarray(this.p, this.p + n)); this.p += n; return s; };
    Pbf.prototype.pular = function (t) {
        if (t === 0) this.varint();
        else if (t === 2) this.p += this.varint();
        else if (t === 5) this.p += 4;
        else if (t === 1) this.p += 8;
    };
    function lerValor(pb, fim) {
        var v = null;
        while (pb.p < fim) {
            var k = pb.varint(), f = k >> 3, t = k & 7;
            if (f === 1) v = pb.str(pb.varint());
            else if (f === 2) { v = new DataView(pb.b.buffer, pb.b.byteOffset + pb.p, 4).getFloat32(0, true); pb.p += 4; }
            else if (f === 3) { v = new DataView(pb.b.buffer, pb.b.byteOffset + pb.p, 8).getFloat64(0, true); pb.p += 8; }
            else if (f === 4 || f === 5) v = pb.varint();
            else if (f === 6) v = pb.svarint();
            else if (f === 7) v = !!pb.varint();
            else pb.pular(t);
        }
        return v;
    }
    function lerMVT(buf, quero) {
        var pb = new Pbf(buf), camadas = {};
        while (pb.p < pb.b.length) {
            var k = pb.varint(), f = k >> 3, t = k & 7;
            if (f !== 3) { pb.pular(t); continue; }
            var fim = pb.varint() + pb.p, Lc = { nome: '', keys: [], vals: [], feats: [], extent: 4096 };
            while (pb.p < fim) {
                var k2 = pb.varint(), f2 = k2 >> 3, t2 = k2 & 7;
                if (f2 === 1) Lc.nome = pb.str(pb.varint());
                else if (f2 === 3) Lc.keys.push(pb.str(pb.varint()));
                else if (f2 === 4) { var e = pb.varint() + pb.p; Lc.vals.push(lerValor(pb, e)); }
                else if (f2 === 5) Lc.extent = pb.varint();
                else if (f2 === 2) {
                    var fe = pb.varint() + pb.p, F = { tags: [], tipo: 0, geom: [] };
                    while (pb.p < fe) {
                        var k3 = pb.varint(), f3 = k3 >> 3, t3 = k3 & 7;
                        if (f3 === 2 || f3 === 4) {
                            var pe = pb.varint() + pb.p, arr = [];
                            while (pb.p < pe) arr.push(pb.varint());
                            if (f3 === 2) F.tags = arr; else F.geom = arr;
                        } else if (f3 === 3) F.tipo = pb.varint();
                        else pb.pular(t3);
                    }
                    Lc.feats.push(F);
                } else pb.pular(t2);
            }
            if (!quero || quero[Lc.nome]) camadas[Lc.nome] = Lc;
        }
        return camadas;
    }
    function propsMVT(Lc, F) {
        var o = {};
        for (var i = 0; i < F.tags.length; i += 2) o[Lc.keys[F.tags[i]]] = Lc.vals[F.tags[i + 1]];
        return o;
    }
    function linhasMVT(F) {
        var out = [], cur = null, x = 0, y = 0, g = F.geom, i = 0;
        while (i < g.length) {
            var cmd = g[i] & 7, n = g[i] >> 3; i++;
            for (var r = 0; r < n; r++) {
                if (cmd === 1 || cmd === 2) {
                    var dx = g[i++], dy = g[i++];
                    x += (dx >> 1) ^ (-(dx & 1));
                    y += (dy >> 1) ^ (-(dy & 1));
                    if (cmd === 1) { cur = [[x, y]]; out.push(cur); }
                    else cur.push([x, y]);
                }
            }
        }
        return out;
    }
    function recortar(a, b, ext) {
        var t0 = 0, t1 = 1, dx = b[0] - a[0], dy = b[1] - a[1];
        var p = [-dx, dx, -dy, dy], q = [a[0], ext - a[0], a[1], ext - a[1]];
        for (var i = 0; i < 4; i++) {
            if (p[i] === 0) { if (q[i] < 0) return null; }
            else {
                var r = q[i] / p[i];
                if (p[i] < 0) { if (r > t1) return null; if (r > t0) t0 = r; }
                else { if (r < t0) return null; if (r < t1) t1 = r; }
            }
        }
        return [[a[0] + t0 * dx, a[1] + t0 * dy], [a[0] + t1 * dx, a[1] + t1 * dy]];
    }
    function blocoParaLatLng(tx, ty, ext) {
        var n = Math.pow(2, MALHA_Z);
        return function (p) {
            var x = (tx + p[0] / ext) / n, y = (ty + p[1] / ext) / n;
            return [Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI, x * 360 - 180];
        };
    }
    function blocoDe(lat, lng) {
        var n = Math.pow(2, MALHA_Z), r = lat * Math.PI / 180;
        return [Math.floor((lng + 180) / 360 * n), Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n)];
    }
    function urlMalha() {
        if (malha.url) return Promise.resolve(malha.url);
        if (malha._pUrl) return malha._pUrl;
        malha._pUrl = fetch(MALHA_TILEJSON)
            .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(function (j) { malha.url = j.tiles[0]; return malha.url; })
            .catch(function (e) { malha._pUrl = null; throw e; });
        return malha._pUrl;
    }

    var _tMalha = null;
    function agendarMalha() {
        clearTimeout(_tMalha);
        _tMalha = setTimeout(carregarMalhaVisivel, 250);
    }
    function limitesSetor(c) {
        var st = D && D.setores[c];
        if (!st) return null;
        var a = 90, b = 180, cc = -90, d = -180;
        st.fids.forEach(function (f) {
            D.segs[f].ll.forEach(function (p) {
                if (p[0] < a) a = p[0]; if (p[0] > cc) cc = p[0];
                if (p[1] < b) b = p[1]; if (p[1] > d) d = p[1];
            });
        });
        return [a, b, cc, d];
    }
    function blocosDaArea(s, w, n, e) {
        var p1 = blocoDe(n, w), p2 = blocoDe(s, e), out = [];
        for (var x = p1[0]; x <= p2[0]; x++) {
            for (var y = p1[1]; y <= p2[1]; y++) out.push([x, y]);
        }
        return out;
    }
    function carregarMalhaVisivel() {
        var m = getMap();
        if (!correcaoAtiva || modo !== 'add' || !m) return;
        if (m.getZoom() < 13) { avisoMapa('Aproxime o mapa para ver as ruas que podem ser adicionadas.'); return; }
        var b = m.getBounds().pad(0.15);
        var lista = blocosDaArea(b.getSouth(), b.getWest(), b.getNorth(), b.getEast());
        if (setorFoco) {
            var bb = limitesSetor(setorFoco);
            if (bb) {
                var ls = blocosDaArea(bb[0] - 0.003, bb[1] - 0.003, bb[2] + 0.003, bb[3] + 0.003);
                if (ls.length <= 40) lista = lista.concat(ls);
            }
        }
        buscarBlocos(lista, b);
    }
    function preCarregarMalhaSetor() {
        if (!correcaoAtiva || modo !== 'add' || !setorFoco) return;
        var bb = limitesSetor(setorFoco);
        if (!bb) return;
        var ls = blocosDaArea(bb[0] - 0.003, bb[1] - 0.003, bb[2] + 0.003, bb[3] + 0.003);
        if (ls.length <= 40) buscarBlocos(ls, null);
    }
    function buscarBlocos(lista, boundsFallback) {
        var novos = lista.filter(function (t) {
            var k = t[0] + '/' + t[1];
            if (malha.blocos[k]) return false;
            malha.blocos[k] = 'carregando';
            return true;
        });
        if (!novos.length) { if (!malha.pendentes) avisoMapa(null); return; }
        malha.pendentes += novos.length;
        avisoMapa('Carregando ruas da cidade… ' + malha.pendentes);

        urlMalha().then(function (url) {
            novos.forEach(function (t) {
                var k = t[0] + '/' + t[1];
                fetch(url.replace('{z}', MALHA_Z).replace('{x}', t[0]).replace('{y}', t[1]))
                    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
                    .then(function (buf) { incluirBlocoMVT(t[0], t[1], buf); malha.blocos[k] = 'ok'; })
                    .catch(function () { delete malha.blocos[k]; malha.falhou = true; })
                    .then(function () {
                        malha.pendentes--;
                        if (malha.pendentes <= 0) {
                            if (malha.falhou && boundsFallback) carregarMalhaFallbackServidor(boundsFallback);
                            else avisoMapa(null);
                        } else {
                            avisoMapa('Carregando ruas da cidade… ' + malha.pendentes);
                        }
                    });
            });
        }).catch(function () {
            novos.forEach(function (t) { delete malha.blocos[t[0] + '/' + t[1]]; });
            malha.pendentes -= novos.length;
            if (boundsFallback) carregarMalhaFallbackServidor(boundsFallback);
            else avisoMapa(null);
        });
    }

    function carregarMalhaFallbackServidor(bounds) {
        if (!D || !D.contratoId) { avisoMapa(null); return; }
        var sh = window.__SISTEMA_LC__ ? window.__SISTEMA_LC__.getSharedInfo() : {};
        var bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(',');
        var qs = 'cidade=' + encodeURIComponent(D.contratoId) + '&bbox=' + encodeURIComponent(bbox);
        if (sh.snapshotId) qs += '&snapshot_id=' + encodeURIComponent(sh.snapshotId);
        if (sh.token) qs += '&token=' + encodeURIComponent(sh.token);
        fetch('/api/correcao-rotas/malha-candidata?' + qs)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (gj) {
                avisoMapa(null);
                if (!gj || !Array.isArray(gj.features)) return;
                gj.features.forEach(function (f) {
                    if (!f.geometry || !f.geometry.coordinates) return;
                    var ll = f.geometry.coordinates.map(function (c) { return [c[1], c[0]]; });
                    if (ll.length < 2) return;
                    var id = ll.map(function (c) { return c[0].toFixed(6) + ',' + c[1].toFixed(6); }).join(';');
                    if (malha.arestas[id]) return;
                    var pr = f.properties || {};
                    var a = { id: id, nome: pr.nome || pr.name || '', via: pr.highway || 'residential', ll: ll };
                    a.len = compLinha(ll);
                    if (a.len < 1) return;
                    a.meio = meioLinha(ll);
                    malha.arestas[id] = a;
                    a.layer = L.polyline(ll, { renderer: rendMalha, color: '#5a5a5a', weight: 3, opacity: 0.55, interactive: false });
                    camadaMalha.addLayer(a.layer);
                    indexar(indiceMalha || (indiceMalha = {}), id, ll);
                });
            })
            .catch(function () { avisoMapa(null); });
    }

    function incluirBlocoMVT(tx, ty, buf) {
        var cam = lerMVT(buf, { transportation: 1, transportation_name: 1 }), T = cam.transportation;
        if (!T) return;
        var ext = T.extent, conv = blocoParaLatLng(tx, ty, ext);
        var linhas = [];
        T.feats.forEach(function (F) {
            if (F.tipo !== 2) return;
            var p = propsMVT(T, F);
            if (!CLASSES_VIA[p['class']] || p.subclass === 'platform' || p.subclass === 'corridor') return;
            linhasMVT(F).forEach(function (l) {
                var atual = null;
                for (var i = 1; i < l.length; i++) {
                    var c = recortar(l[i - 1], l[i], ext);
                    if (!c) { atual = null; continue; }
                    if (!atual || atual[atual.length - 1][0] !== c[0][0] || atual[atual.length - 1][1] !== c[0][1]) {
                        atual = [c[0]];
                        linhas.push({ pts: atual, via: p.subclass || p['class'] });
                    }
                    atual.push(c[1]);
                }
            });
        });
        var uso = {};
        linhas.forEach(function (L2) {
            L2.pts.forEach(function (q, i) {
                var k = Math.round(q[0]) + ',' + Math.round(q[1]);
                uso[k] = (uso[k] || 0) + ((i === 0 || i === L2.pts.length - 1) ? 2 : 1);
            });
        });
        var N = cam.transportation_name, grade = {}, CELB = 64;
        if (N) {
            N.feats.forEach(function (F) {
                var nome = propsMVT(N, F).name;
                if (!nome) return;
                linhasMVT(F).forEach(function (l) {
                    for (var i = 1; i < l.length; i++) {
                        var a = l[i - 1], b = l[i];
                        for (var gx = Math.floor(Math.min(a[0], b[0]) / CELB); gx <= Math.floor(Math.max(a[0], b[0]) / CELB); gx++) {
                            for (var gy = Math.floor(Math.min(a[1], b[1]) / CELB); gy <= Math.floor(Math.max(a[1], b[1]) / CELB); gy++) {
                                (grade[gx + ':' + gy] || (grade[gx + ':' + gy] = [])).push([a, b, nome]);
                            }
                        }
                    }
                });
            });
        }
        function nomePerto(q) {
            var best = null, bd = 6;
            (grade[Math.floor(q[0] / CELB) + ':' + Math.floor(q[1] / CELB)] || []).forEach(function (sg) {
                var a = sg[0], b = sg[1], dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy;
                var t = L2 ? Math.max(0, Math.min(1, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / L2)) : 0;
                var x = a[0] + t * dx - q[0], y = a[1] + t * dy - q[1], d = Math.sqrt(x * x + y * y);
                if (d < bd) { bd = d; best = sg[2]; }
            });
            return best || '';
        }
        linhas.forEach(function (L2) {
            var pts = L2.pts, ini = 0;
            for (var i = 1; i < pts.length; i++) {
                var kk = Math.round(pts[i][0]) + ',' + Math.round(pts[i][1]);
                if (i === pts.length - 1 || uso[kk] > 1) {
                    var parte = pts.slice(ini, i + 1); ini = i;
                    var ll = parte.map(conv);
                    if (ll.length < 2) continue;
                    var meioT = parte[Math.floor(parte.length / 2)];
                    var id = ll.map(function (c) { return c[0].toFixed(6) + ',' + c[1].toFixed(6); }).join(';');
                    if (malha.arestas[id]) continue;
                    var a = { id: id, nome: '', via: L2.via, ll: ll };
                    a.len = compLinha(ll);
                    if (a.len < 1) continue;
                    a.meio = meioLinha(ll);
                    var mT = parte.length === 2 ? [(parte[0][0] + parte[1][0]) / 2, (parte[0][1] + parte[1][1]) / 2] : meioT;
                    a.nome = nomePerto(mT);
                    malha.arestas[id] = a;
                    var fina = /footway|path|steps|track|pedestrian|cycleway|bridleway/.test(a.via);
                    a.layer = L.polyline(ll, {
                        renderer: rendMalha,
                        color: '#5a5a5a',
                        weight: fina ? 2 : 3,
                        opacity: 0.55,
                        dashArray: fina ? '4,4' : null,
                        interactive: false
                    });
                    camadaMalha.addLayer(a.layer);
                    indexar(indiceMalha || (indiceMalha = {}), id, ll);
                }
            }
        });
    }

    // ======================================================================
    // SELEÇÃO, PEDIDOS E RASCUNHO LOCAL
    // ======================================================================
    function desenharSelecao() {
        if (!camadaSel || !D) return;
        camadaSel.clearLayers();
        Object.keys(selecao).forEach(function (fid) {
            var s = D.segs[fid];
            if (!s) return;
            L.polyline(s.ll, { renderer: rendTopo, color: '#111', weight: 10, opacity: 0.85, interactive: false }).addTo(camadaSel);
            L.polyline(s.ll, { renderer: rendTopo, color: '#ffe600', weight: 5, opacity: 1, interactive: false }).addTo(camadaSel);
        });
        Object.keys(selMalha).forEach(function (id) {
            var a = selMalha[id];
            if (!a) return;
            L.polyline(a.ll, { renderer: rendTopo, color: '#111', weight: 10, opacity: 0.85, interactive: false }).addTo(camadaSel);
            L.polyline(a.ll, { renderer: rendTopo, color: '#ff4fb0', weight: 5, opacity: 1, interactive: false }).addTo(camadaSel);
        });
    }

    function linhasDoPedido(p) {
        if (p.tipo === 'adicionar') return (p.ruas || []).map(function (r) { return r.ll; });
        return (p.fids || []).map(function (f) { return D.segs[f] ? D.segs[f].ll : null; }).filter(Boolean);
    }

    function desenharPedidosMapa() {
        if (!camadaPed || !D) return;
        camadaPed.clearLayers();
        pedidos.forEach(function (p) {
            var ac = ACOES[p.tipo];
            if (!ac) return;
            var traco = (p.tipo === 'trajeto' || p.tipo === 'temp') ? '8,6' : null;
            linhasDoPedido(p).forEach(function (ll) {
                var apag = setorFoco && p.setor !== setorFoco && p.destino !== setorFoco;
                L.polyline(ll, { renderer: rendTopo, color: '#fff', weight: 9, opacity: apag ? 0.2 : 0.9, interactive: false }).addTo(camadaPed);
                L.polyline(ll, { renderer: rendTopo, color: ac.cor, weight: 5, opacity: apag ? 0.25 : 1, dashArray: traco, interactive: false }).addTo(camadaPed);
            });
        });
    }

    function pedidoDoTrecho(fid) {
        for (var i = 0; i < pedidos.length; i++) {
            if (pedidos[i].fids && pedidos[i].fids.indexOf(Number(fid)) >= 0) return pedidos[i];
        }
        return null;
    }

    function chaveRascunho() {
        return 'lcCorrecaoRotas:' + (D ? (D.contratoId + ':' + D.dataIso + ':' + D.periodoRaw) : '');
    }
    function salvarRascunho() {
        if (!D) return;
        try {
            localStorage.setItem(chaveRascunho(), JSON.stringify({
                pedidos: pedidos,
                nome: ($('crNomeSolic') && $('crNomeSolic').value) || '',
                obsGeral: ($('crObsGeral') && $('crObsGeral').value) || ''
            }));
        } catch (e) {}
    }
    function carregarRascunho() {
        pedidos = [];
        if (!D) return;
        try {
            var r = JSON.parse(localStorage.getItem(chaveRascunho()) || 'null');
            if (r) {
                pedidos = (r.pedidos || []).filter(function (p) {
                    return p.tipo === 'adicionar' || (p.fids || []).every(function (f) { return Boolean(D.segs[f]); });
                });
                if (r.nome && $('crNomeSolic')) $('crNomeSolic').value = r.nome;
                if ($('crObsGeral')) $('crObsGeral').value = r.obsGeral || '';
            }
        } catch (e) {}
    }

    // ======================================================================
    // RENDERIZAÇÃO DA ABA LATERAL
    // ======================================================================
    function iniciarPainelCorrecao() {
        if (!D) return;
        $('crTopoSub').textContent = D.contrato + (D.periodo === 'dia' ? ' · Dia ' + D.de : ' · ' + D.de + (D.ate && D.ate !== D.de ? ' a ' + D.ate : ''));
        selecao = {};
        selMalha = {};
        setorFoco = null;
        turnoFiltro = 'todos';
        modo = 'clique';

        // Turnos
        var tns = {};
        D.codigos.forEach(function (c) { tns[D.setores[c].turno] = 1; });
        var ordem = ['Diurno', 'Noturno'].filter(function (t) { return tns[t]; });
        var ch = $('crChipsTurno');
        ch.innerHTML = '';
        [['todos', 'Todos']].concat(ordem.map(function (t) { return [t, t]; })).forEach(function (t) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'cr-chip' + (t[0] === 'todos' ? ' ativo' : '');
            b.textContent = t[1];
            b.dataset.t = t[0];
            b.onclick = function () {
                turnoFiltro = t[0];
                Array.prototype.forEach.call(ch.children, function (x) { x.classList.toggle('ativo', x === b); });
                if (setorFoco && !setorVisivel(setorFoco)) setorFoco = null;
                aplicarFiltrosCorrecao(true);
                renderSelecao();
            };
            ch.appendChild(b);
        });

        // Chips Mostrar Ruas
        var boxR = $('crChipsRuas');
        boxR.innerHTML = '';
        filtroRuas = 'todas';
        FILTROS_RUAS[D.periodo === 'dia' ? 'dia' : 'mes'].forEach(function (f) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'cr-chip' + (f[0] === 'todas' ? ' ativo' : '');
            b.textContent = f[1];
            b.onclick = function () {
                filtroRuas = f[0];
                Array.prototype.forEach.call(boxR.children, function (x) { x.classList.toggle('ativo', x === b); });
                aplicarFiltrosCorrecao(false);
                renderSelecao();
                var n = D.lista.filter(segAtivo).length;
                if (filtroRuas !== 'todas') {
                    crToast(n ? n + ' trecho(s) aparecem com este filtro.' : 'Nenhuma rua com este filtro' + (setorFoco ? ' no ' + setorFoco : '') + '.');
                }
            };
            boxR.appendChild(b);
        });

        // Datalist de ruas
        var dl = $('crListaRuas');
        dl.innerHTML = '';
        D.ruas.forEach(function (r) {
            var o = document.createElement('option');
            o.value = r;
            dl.appendChild(o);
        });

        Array.prototype.forEach.call(document.querySelectorAll('#crPainelLateral .cr-modo'), function (b) {
            b.classList.toggle('ativo', b.dataset.modo === 'clique');
        });

        carregarRascunho();
        desenharSegmentos();
        renderSelecao();
        renderPedidos();
    }

    function renderListaSetores() {
        var box = $('crListaSetores');
        if (!box || !D) return;
        var cont = {};
        pedidos.forEach(function (p) {
            cont[p.setor] = (cont[p.setor] || 0) + 1;
            if (p.destino && p.fids && p.fids.length > (p.jaNoDestino || 0)) {
                cont[p.destino] = (cont[p.destino] || 0) + 1;
            }
        });
        var h = '<div class="cr-setor-item' + (!setorFoco ? ' ativo' : '') + '" data-c=""><span class="bol" style="background:#ccc"></span><span class="nome">Todos os setores</span></div>';
        D.codigos.forEach(function (c) {
            var s = D.setores[c];
            if (!setorVisivel(c)) return;
            h += '<div class="cr-setor-item' + (setorFoco === c ? ' ativo' : '') + '" data-c="' + esc(c) + '">' +
                 '<span class="bol" style="background:' + esc(s.cor) + '"></span>' +
                 '<span class="nome">' + esc(c) + (s.agenda ? ' <span class="ag">· ' + esc(s.agenda) + '</span>' : '') + '</span>' +
                 (cont[c] ? '<span class="qt">' + cont[c] + '</span>' : '') +
                 '</div>';
        });
        box.innerHTML = h;
        Array.prototype.forEach.call(box.querySelectorAll('.cr-setor-item'), function (el) {
            el.onclick = function () { focarSetor(el.dataset.c || null, false); };
        });
    }

    function focarSetor(c, semEnquadrar) {
        if (c !== setorFoco) selecao = {};
        setorFoco = c;
        if (modo === 'add') { selMalha = {}; preCarregarMalhaSetor(); }
        aplicarFiltrosCorrecao(!semEnquadrar && Boolean(c));
        renderSelecao();
    }

    function atualizarDicaModo() {
        var el = $('crDicaModo');
        if (!el) return;
        var t = {
            clique: 'Clique nas ruas do mapa que deseja alterar.',
            laco: 'Segure o botão do mouse e desenhe um círculo em volta das ruas.',
            add: setorFoco
                ? 'Aproxime o mapa e clique nas ruas cinzas que devem entrar no <b>' + esc(setorFoco) + '</b>.'
                : 'Primeiro escolha na lista acima o setor que vai receber as ruas.'
        }[modo];
        el.innerHTML = t;
    }

    function trocarModo(mNovo) {
        var m = getMap();
        modo = mNovo;
        Array.prototype.forEach.call(document.querySelectorAll('#crPainelLateral .cr-modo'), function (b) {
            b.classList.toggle('ativo', b.dataset.modo === mNovo);
        });
        if (m) {
            if (mNovo === 'laco') m.dragging.disable();
            else m.dragging.enable();
            m.getContainer().style.cursor = mNovo === 'laco' ? 'crosshair' : '';
            if (mNovo === 'add') {
                selecao = {};
                camadaMalha.addTo(m);
                preCarregarMalhaSetor();
                agendarMalha();
            } else {
                selMalha = {};
                avisoMapa(null);
                if (m.hasLayer(camadaMalha)) m.removeLayer(camadaMalha);
            }
        }
        desenharSelecao();
        renderSelecao();
        atualizarDicaModo();
        desenharLegenda();
    }

    function renderSelecao() {
        var box = $('crSelecao');
        if (!box || !D) return;
        var fids = Object.keys(selecao), arestas = Object.keys(selMalha).map(function (k) { return selMalha[k]; });
        if (modo === 'add') {
            if (!arestas.length) { box.style.display = 'none'; desenharLegenda(); return; }
            var comp = arestas.reduce(function (a, x) { return a + x.len; }, 0);
            box.style.display = '';
            box.innerHTML =
                '<div class="resumo"><b>' + arestas.length + ' rua(s) escolhida(s)</b> · ' + fmtM(comp) + '</div>' +
                '<div class="ruas">' + esc(resumoRuas(nomesRuas(arestas.map(function (a) { return a.nome; })), 4)) + '</div>' +
                '<div class="cr-acoes"><button type="button" class="cr-acao" style="border-left-color:' + ACOES.adicionar.cor + '" data-a="adicionar">' +
                ACOES.adicionar.ic + '<span>Adicionar ao setor ' + esc(setorFoco || '') + '<small>' + ACOES.adicionar.desc + '</small></span></button></div>' +
                '<div style="margin-top:8px;text-align:right"><button type="button" class="cr-btn-link" id="crBtLimpaSel">Limpar seleção</button></div>';
        } else {
            if (!fids.length) { box.style.display = 'none'; desenharLegenda(); return; }
            var segs = fids.map(function (f) { return D.segs[f]; }).filter(Boolean);
            var comp2 = segs.reduce(function (a, s) { return a + s.len; }, 0);
            var temManual = segs.some(function (s) { return s.cls === 'manual'; });
            var h = '<div class="resumo"><b>' + segs.length + ' trecho(s) selecionado(s)</b> · ' + fmtM(comp2) + '</div>' +
                    '<div class="ruas">' + esc(resumoRuas(nomesRuas(segs.map(function (s) { return s.rua; })), 4)) + '</div>';
            var jaPed = segs.filter(function (s) { return pedidoDoTrecho(s.fid); });
            if (jaPed.length) {
                h += '<div class="cr-alerta info">' + jaPed.length + ' trecho(s) já estão na solicitação. ' +
                     '<button type="button" class="cr-btn-link" id="crBtTirar">Tirar da solicitação</button></div>';
            }
            h += '<div class="cr-acoes">';
            ['manual', 'trajeto', 'mover', 'temp'].concat(temManual ? ['normal'] : []).forEach(function (k) {
                var a = ACOES[k];
                h += '<button type="button" class="cr-acao" style="border-left-color:' + a.cor + '" data-a="' + k + '">' +
                     a.ic + '<span>' + a.nome + '<small>' + a.desc + '</small></span></button>';
            });
            h += '</div><div style="margin-top:8px;text-align:right"><button type="button" class="cr-btn-link" id="crBtLimpaSel">Limpar seleção</button></div>';
            box.style.display = '';
            box.innerHTML = h;
        }
        Array.prototype.forEach.call(box.querySelectorAll('.cr-acao'), function (b) {
            b.onclick = function () { abrirModalAcao(b.dataset.a); };
        });
        desenharLegenda();
        $('crBtLimpaSel').onclick = function () { selecao = {}; selMalha = {}; desenharSelecao(); renderSelecao(); };
        if ($('crBtTirar')) {
            $('crBtTirar').onclick = function () {
                var tirar = Object.keys(selecao).map(Number), n = 0;
                pedidos.forEach(function (p) {
                    if (!p.fids) return;
                    var a = p.fids.length;
                    p.fids = p.fids.filter(function (f) { return tirar.indexOf(f) < 0; });
                    n += a - p.fids.length;
                });
                pedidos = pedidos.filter(function (p) { return p.tipo === 'adicionar' || (p.fids && p.fids.length); });
                selecao = {};
                salvarRascunho();
                desenharSelecao();
                desenharPedidosMapa();
                renderSelecao();
                renderPedidos();
                renderListaSetores();
                crToast(n + ' trecho(s) retirados da solicitação.');
            };
        }
    }

    // ======================================================================
    // MODAL DE AÇÃO E LISTA DE SOLICITAÇÕES
    // ======================================================================
    function abrirCrModal(html, lateral) {
        $('crModalBox').innerHTML = html;
        $('crModalFundo').classList.toggle('lateral', Boolean(lateral));
        $('crModalFundo').style.display = 'flex';
    }
    function fecharCrModal() {
        $('crModalFundo').style.display = 'none';
    }

    function abrirModalAcao(tipo) {
        var a = ACOES[tipo], h = '<h2>' + a.ic + ' ' + a.nome + '</h2>';
        if (tipo === 'adicionar') {
            var ars = Object.keys(selMalha).map(function (k) { return selMalha[k]; });
            var outras = {}, nOut = 0;
            ars.forEach(function (x) {
                var cs = codigosNaRota(x).filter(function (c) { return c !== setorFoco; });
                if (cs.length) { nOut++; cs.forEach(function (c) { outras[c] = 1; }); }
            });
            if (nOut) h += '<div class="cr-alerta">⚠️ Já faz parte do ' + esc(Object.keys(outras).join(', ')) + '. Para tirar de lá, use “Mudar de setor”.</div>';
            h += '<label class="cr-rot">Coleta</label><select class="cr-select" id="crFCls"><option value="coleta">Normal (caminhão)</option><option value="manual">Manual (a pé)</option></select>';
        } else {
            var segs = Object.keys(selecao).map(function (f) { return D.segs[f]; }).filter(Boolean);
            var cods = nomesRuas(segs.map(function (s) { return s.code; }));
            if (tipo === 'mover') {
                h += '<label class="cr-rot">Para qual setor? *</label><select class="cr-select" id="crFDestino"><option value="">Escolha o setor</option>';
                D.codigos.forEach(function (c) {
                    if (cods.length === 1 && c === cods[0]) return;
                    h += '<option value="' + esc(c) + '">' + esc(c) + (D.setores[c].agenda ? ' — ' + esc(D.setores[c].agenda) : '') + '</option>';
                });
                var extras = (D.setoresConhecidos || []).filter(function (c) { return !D.setores[c]; })
                    .sort(function (x, y) { return x.localeCompare(y, 'pt-BR', { numeric: true }); });
                extras.forEach(function (c) {
                    var ag = (D.setoresConhecidosMeta[c] && D.setoresConhecidosMeta[c].agenda) || '';
                    h += '<option value="' + esc(c) + '">' + esc(c) + (ag ? ' — ' + esc(ag) : ' (outro dia)') + '</option>';
                });
                h += '<option value="__outro">Outro setor…</option></select>' +
                     '<input type="text" class="cr-input" id="crFDestinoOutro" placeholder="Código do setor (ex.: SCDD 02)" style="display:none;margin-top:6px">' +
                     '<div id="crAvisoDestino" style="margin-top:8px"></div>';
            }
            if (tipo === 'temp') {
                h += '<div class="cr-linha"><div><label class="cr-rot">Fecha em *</label><input type="date" class="cr-input" id="crFDe" value="' + hojeISO() + '"></div>' +
                     '<div><label class="cr-rot">Volta em *</label><input type="date" class="cr-input" id="crFAte"></div></div>';
            }
        }
        var placeholders = {
            manual: 'Ex.: rua estreita, lixeiras do outro lado da praça',
            trajeto: 'Ex.: avenida só de passagem, sem residências',
            mover: 'Ex.: essa rua pertence ao bairro do outro setor',
            temp: 'Ex.: carnaval, obra da prefeitura',
            normal: 'Ex.: rua foi alargada e o caminhão já entra',
            adicionar: 'Ex.: loteamento novo, ruas sem coleta'
        };
        h += '<label class="cr-rot">Motivo' + (tipo === 'temp' ? ' *' : ' (opcional)') + '</label>' +
             '<textarea class="cr-textarea" id="crFObs" placeholder="' + placeholders[tipo] + '"></textarea>';
        h += '<div class="rodape"><button type="button" class="cr-btn-sec" id="crMCancelar">Cancelar</button><button type="button" class="cr-btn" id="crMOk">Confirmar</button></div>';
        abrirCrModal(h, true);

        if (tipo === 'mover') {
            $('crFDestino').onchange = function () {
                $('crFDestinoOutro').style.display = this.value === '__outro' ? '' : 'none';
                var dest = this.value;
                var sgs = Object.keys(selecao).map(function (f) { return D.segs[f]; }).filter(Boolean);
                var ja = sgs.filter(function (s) { return s.shared.indexOf(dest) >= 0; }).length;
                $('crAvisoDestino').innerHTML = ja
                    ? '<div class="cr-alerta">⚠️ ' + (ja === sgs.length ? 'Essa rua já faz' : 'Parte dessas ruas já faz') + ' parte do ' + esc(dest) + '. Ela só vai sair do setor atual.</div>'
                    : '';
            };
        }
        $('crMCancelar').onclick = fecharCrModal;
        $('crMOk').onclick = function () { confirmarAcao(tipo); };
    }

    function confirmarAcao(tipo) {
        var obs = ($('crFObs').value || '').trim();
        if (tipo === 'adicionar') {
            var ars = Object.keys(selMalha).map(function (k) {
                var a = selMalha[k];
                return { id: a.id, nome: a.nome, via: a.via, ll: a.ll, len: a.len, meio: a.meio };
            });
            pedidos.push({
                id: uid(),
                tipo: 'adicionar',
                acao_nome: ACOES.adicionar.nome,
                setor: setorFoco,
                ruas: ars,
                ruas_nomes: nomesRuas(ars.map(function (r) { return r.nome; })),
                trechos_qtd: ars.length,
                extensao_m: Math.round(ars.reduce(function (acc, r) { return acc + r.len; }, 0) * 10) / 10,
                cls: $('crFCls').value,
                obs: obs,
                criado: new Date().toISOString()
            });
            selMalha = {};
        } else {
            var destino = null, de = null, ate = null;
            if (tipo === 'mover') {
                destino = $('crFDestino').value;
                if (destino === '__outro') destino = ($('crFDestinoOutro').value || '').trim().toUpperCase().replace(/\s+/g, ' ');
                if (!destino) { crToast('Escolha o setor de destino.'); return; }
            }
            if (tipo === 'temp') {
                de = $('crFDe').value;
                ate = $('crFAte').value;
                if (!de || !ate) { crToast('Informe as duas datas da interdição.'); return; }
                if (ate < de) { crToast('A data de volta deve ser depois da data de fechamento.'); return; }
                if (!obs) { crToast('Informe o motivo da interdição.'); return; }
            }
            var porSetor = {};
            Object.keys(selecao).forEach(function (f) {
                var s = D.segs[f];
                if (s) (porSetor[s.code] || (porSetor[s.code] = [])).push(s.fid);
            });
            var substituidos = 0;
            Object.keys(porSetor).forEach(function (cod) {
                var fids = porSetor[cod];
                pedidos.forEach(function (p) {
                    if (!p.fids) return;
                    var antes = p.fids.length;
                    p.fids = p.fids.filter(function (f) { return fids.indexOf(f) < 0; });
                    substituidos += antes - p.fids.length;
                });
                if (tipo === 'mover' && destino === cod) return;
                var jaNoDestino = destino ? fids.filter(function (f) { return D.segs[f].shared.indexOf(destino) >= 0; }).length : 0;
                var sgs = fids.map(function (f) { return D.segs[f]; });
                pedidos.push({
                    id: uid(),
                    tipo: tipo,
                    acao_nome: ACOES[tipo].nome,
                    setor: cod,
                    destino: destino,
                    de: de,
                    ate: ate,
                    fids: fids,
                    ruas_nomes: nomesRuas(sgs.map(function (s) { return s.rua; })),
                    trechos_qtd: fids.length,
                    extensao_m: Math.round(sgs.reduce(function (acc, s) { return acc + s.len; }, 0) * 10) / 10,
                    obs: obs,
                    jaNoDestino: jaNoDestino,
                    criado: new Date().toISOString()
                });
            });
            pedidos = pedidos.filter(function (p) { return p.tipo === 'adicionar' || (p.fids && p.fids.length); });
            if (substituidos) crToast(substituidos + ' trecho(s) já tinham outro pedido: vale o novo.');
            selecao = {};
        }
        fecharCrModal();
        salvarRascunho();
        desenharSelecao();
        desenharPedidosMapa();
        renderSelecao();
        renderPedidos();
        renderListaSetores();
        desenharLegenda();
    }

    function descrPedido(p) {
        var nomes, comp, n;
        if (p.tipo === 'adicionar') {
            nomes = nomesRuas((p.ruas || []).map(function (r) { return r.nome; }));
            comp = (p.ruas || []).reduce(function (a, r) { return a + r.len; }, 0);
            n = (p.ruas || []).length;
        } else {
            var segs = (p.fids || []).map(function (f) { return D.segs[f]; }).filter(Boolean);
            nomes = segs.length ? nomesRuas(segs.map(function (s) { return s.rua; })) : (p.ruas_nomes || []);
            comp = segs.length ? segs.reduce(function (a, s) { return a + s.len; }, 0) : Number(p.extensao_m || 0);
            n = segs.length || Number(p.trechos_qtd || 0);
        }
        var det = '';
        if (p.tipo === 'mover') det = 'De ' + p.setor + ' para ' + p.destino + (p.jaNoDestino ? ' (' + p.jaNoDestino + ' trecho(s) já estão no ' + p.destino + ')' : '');
        if (p.tipo === 'temp') det = 'Fechada de ' + fmtData(p.de) + ' até ' + fmtData(p.ate);
        if (p.tipo === 'adicionar') det = 'Entra no ' + p.setor + ' como ' + (p.cls === 'manual' ? 'coleta manual' : 'coleta normal');
        return { nomes: nomes, comp: comp, n: n, det: det };
    }

    function renderPedidos() {
        $('crContPedidos').textContent = pedidos.length;
        $('crBtPdf').disabled = !pedidos.length;
        $('crBtApagarTudo').style.display = pedidos.length ? '' : 'none';
        var box = $('crPedidos');
        if (!pedidos.length) {
            box.innerHTML = '<div class="cr-bloco cr-vazio">Nenhuma alteração ainda. Selecione ruas no mapa e escolha o que deve mudar.</div>';
            return;
        }
        var grupos = {};
        pedidos.forEach(function (p) { (grupos[p.setor] || (grupos[p.setor] = [])).push(p); });
        var h = '';
        Object.keys(grupos).sort(function (a, b) { return a.localeCompare(b, 'pt-BR', { numeric: true }); }).forEach(function (c) {
            h += '<div class="cr-grupo-setor">Setor ' + esc(c) + '</div>';
            grupos[c].forEach(function (p) {
                var a = ACOES[p.tipo] || ACOES.manual, d = descrPedido(p);
                h += '<div class="cr-pedido" style="border-left-color:' + a.cor + '"><div class="t">' + a.ic + ' ' + a.nome + '</div>' +
                     '<div class="m">' + d.n + ' trecho(s) · ' + fmtM(d.comp) + ' · ' + esc(resumoRuas(d.nomes, 3)) + '</div>' +
                     (d.det ? '<div class="m"><b>' + esc(d.det) + '</b></div>' : '') +
                     (p.obs ? '<div class="m">“' + esc(p.obs) + '”</div>' : '') +
                     '<div class="bt"><button type="button" class="cr-btn-link" data-ver="' + p.id + '">Ver no mapa</button>' +
                     '<button type="button" class="cr-btn-link" style="color:#c0392b" data-del="' + p.id + '">Excluir</button></div></div>';
            });
        });
        box.innerHTML = h;
        Array.prototype.forEach.call(box.querySelectorAll('[data-del]'), function (b) {
            b.onclick = function () {
                if (!confirm('Excluir esta alteração da solicitação?')) return;
                pedidos = pedidos.filter(function (p) { return p.id !== b.dataset.del; });
                salvarRascunho();
                desenharPedidosMapa();
                renderPedidos();
                renderListaSetores();
                desenharLegenda();
            };
        });
        Array.prototype.forEach.call(box.querySelectorAll('[data-ver]'), function (b) {
            b.onclick = function () {
                var m = getMap();
                var p = pedidos.filter(function (x) { return x.id === b.dataset.ver; })[0];
                if (!p || !m) return;
                if (setorFoco !== p.setor) {
                    if (!setorVisivel(p.setor)) {
                        turnoFiltro = 'todos';
                        Array.prototype.forEach.call($('crChipsTurno').children, function (x) {
                            x.classList.toggle('ativo', x.dataset.t === 'todos');
                        });
                    }
                    setorFoco = p.setor;
                    aplicarFiltrosCorrecao(false);
                }
                var bb = null;
                linhasDoPedido(p).forEach(function (ll) {
                    ll.forEach(function (pt) {
                        if (!bb) bb = L.latLngBounds(pt, pt);
                        else bb.extend(pt);
                    });
                });
                if (bb) m.fitBounds(bb, { padding: [80, 80], maxZoom: 18 });
            };
        });
    }

    function desenharLegenda() {
        var box = $('crLegendaMapa');
        if (!box || !D) return;
        var h = '<div class="lt">' + (D.periodo === 'dia' ? 'Execução (' + esc(D.de) + ')' : 'Execução no período') + '</div>';
        (D.legenda || []).forEach(function (i) {
            h += '<div class="li"><span class="lc" style="background:' + i[0] + '"></span>' + i[1] + '</div>';
        });
        var usados = {};
        pedidos.forEach(function (p) { usados[p.tipo] = 1; });
        var ks = Object.keys(usados);
        if (ks.length || Object.keys(selecao).length || Object.keys(selMalha).length) {
            h += '<div class="lt" style="margin-top:8px">Sua solicitação</div>';
            if (Object.keys(selecao).length) h += '<div class="li"><span class="lc" style="background:#ffe600;outline:2px solid #111"></span>Selecionado agora</div>';
            if (Object.keys(selMalha).length) h += '<div class="li"><span class="lc" style="background:#ff4fb0;outline:2px solid #111"></span>Rua a adicionar</div>';
            ks.forEach(function (k) {
                var a = ACOES[k];
                var bg = (k === 'trajeto' || k === 'temp') ? 'repeating-linear-gradient(90deg,' + a.cor + ' 0 8px,#fff 8px 12px)' : a.cor;
                h += '<div class="li"><span class="lc" style="background:' + bg + '"></span>' + a.nome + '</div>';
            });
        }
        if (modo === 'add') h += '<div class="li"><span class="lc" style="background:#5a5a5a"></span>Ruas candidatas</div>';
        box.innerHTML = h;
    }

    // ======================================================================
    // GEOJSON TÉCNICO E MOTOR DE PDF A4 PAISAGEM (200 DPI) + ENVIO AUTOMÁTICO
    // ======================================================================
    function montarGeoJSON() {
        var fs = [];
        pedidos.forEach(function (p) {
            var base = {
                acao: p.tipo,
                acao_nome: ACOES[p.tipo].nome,
                setor: p.setor,
                setor_destino: p.destino || null,
                interdicao_de: p.de || null,
                interdicao_ate: p.ate || null,
                observacao: p.obs || '',
                pedido_id: p.id
            };
            if (p.tipo === 'adicionar') {
                p.ruas.forEach(function (r) {
                    fs.push({
                        type: 'Feature',
                        geometry: { type: 'LineString', coordinates: r.ll.map(function (c) { return [c[1], c[0]]; }) },
                        properties: Object.assign({}, base, {
                            classification: p.cls === 'manual' ? 'manual' : 'collection',
                            rua: r.nome,
                            tipo_via: r.via,
                            fonte: 'OpenStreetMap / Malha Local',
                            len_m: Math.round(r.len * 10) / 10
                        })
                    });
                });
            } else {
                p.fids.forEach(function (f) {
                    var s = D.segs[f];
                    if (!s) return;
                    var nova = { manual: 'manual', trajeto: 'travel', normal: 'collection' }[p.tipo] || null;
                    fs.push({
                        type: 'Feature',
                        geometry: { type: 'LineString', coordinates: s.ll.map(function (c) { return [c[1], c[0]]; }) },
                        properties: Object.assign({}, base, {
                            fid_mapa: s.origId,
                            rota: s.rota,
                            rua: s.rua,
                            len_m: Math.round(s.len * 10) / 10,
                            classification_atual: s.cls === 'manual' ? 'manual' : 'collection',
                            classification_nova: nova
                        })
                    });
                });
            }
        });
        return {
            type: 'FeatureCollection',
            name: 'correcoes_' + D.contratoId,
            metadata: {
                contrato: D.contrato,
                contrato_id: D.contratoId,
                mapa: D.titulo,
                solicitante: ($('crNomeSolic').value || '').trim(),
                observacoes_gerais: ($('crObsGeral').value || '').trim(),
                gerado_em: new Date().toISOString(),
                crs: 'EPSG:4326'
            },
            features: fs
        };
    }

    function nomeBaseArquivo() {
        var per = D.periodo === 'dia' ? '_dia_' + (D.de || '').replace(/\//g, '-') : (D.ate ? '_mes_' + D.ate.slice(3).replace('/', '-') : '');
        return 'Solicitacao_Correcao_' + D.contrato.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^A-Za-z0-9]+/g, '_') + per + '_' + hojeISO();
    }

    function baixarBlob(blob, nome) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = nome;
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 2500);
    }

    function blobParaBase64(blob) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onloadend = function () {
                var res = String(reader.result || '');
                var idx = res.indexOf(',');
                resolve(idx >= 0 ? res.slice(idx + 1) : res);
            };
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
    }

    // Motor gráfico de PDF (idêntico ao Correcao_de_Rotas V4.html)
    var PW = 2339, PH = 1654, MG = 80;
    var FONTE = 'Inter, Montserrat, "Segoe UI", Arial, sans-serif';

    function lon2x(lon, z) { return (lon + 180) / 360 * 256 * Math.pow(2, z); }
    function lat2y(lat, z) {
        var s = Math.sin(lat * Math.PI / 180);
        s = Math.max(-0.9999, Math.min(0.9999, s));
        return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * 256 * Math.pow(2, z);
    }
    function carregarImg(src, ms) {
        return new Promise(function (res) {
            var im = new Image(), ok = false;
            if (!/^data:/.test(src) && src.indexOf('/') !== 0) im.crossOrigin = 'anonymous';
            var t = setTimeout(function () { if (!ok) { ok = true; res(null); } }, ms || 7000);
            im.onload = function () { if (!ok) { ok = true; clearTimeout(t); res(im); } };
            im.onerror = function () { if (!ok) { ok = true; clearTimeout(t); res(null); } };
            im.src = src;
        });
    }

    function recebeAlgo(p) { return p.tipo === 'mover' && p.fids && p.fids.length > (p.jaNoDestino || 0); }
    function pedidosDoSetor(c) { return pedidos.filter(function (p) { return p.setor === c || (p.destino === c && recebeAlgo(p)); }); }
    function setoresAfetados() {
        var m = {};
        pedidos.forEach(function (p) {
            m[p.setor] = 1;
            if (p.destino && recebeAlgo(p) && D.setores[p.destino]) m[p.destino] = 1;
        });
        return Object.keys(m).sort(function (a, b) { return a.localeCompare(b, 'pt-BR', { numeric: true }); });
    }
    function estadoTrecho(fid, c) {
        for (var i = 0; i < pedidos.length; i++) {
            var p = pedidos[i];
            if (p.fids && p.setor === c && p.fids.indexOf(Number(fid)) >= 0) return p;
        }
        return null;
    }
    function hexA(hex, a) {
        var h = (hex || '#666666').replace('#', '');
        if (h.length !== 6) return 'rgba(100,100,100,' + a + ')';
        return 'rgba(' + parseInt(h.substr(0, 2), 16) + ',' + parseInt(h.substr(2, 2), 16) + ',' + parseInt(h.substr(4, 2), 16) + ',' + a + ')';
    }

    function desenharMapaPDF(ctx, X, Y, W, H, c, fase, foco) {
        var st = D.setores[c] || { code: c, cor: '#038242', geoms: [], fids: [] };
        var linhas = [];
        var bbPts = [];
        st.fids.forEach(function (fid) {
            var s = D.segs[fid], p = estadoTrecho(fid, c), cor = COR_CLASS[s.cls], traco = null, halo = Boolean(p);
            s.ll.forEach(function (pt) { bbPts.push(pt); });
            if (fase === 'depois' && p) {
                if (p.tipo === 'manual') cor = COR_CLASS.manual;
                else if (p.tipo === 'normal') cor = COR_CLASS.coleta;
                else if (p.tipo === 'trajeto') { cor = ACOES.trajeto.cor; traco = [18, 12]; }
                else if (p.tipo === 'temp') { cor = ACOES.temp.cor; traco = [18, 12]; }
                else if (p.tipo === 'mover') { cor = '#d63031'; traco = [6, 12]; }
            }
            linhas.push({ ll: s.ll, cor: cor, larg: (p && fase === 'depois') ? 8 : 6, traco: traco, halo: halo, ordem: p ? 2 : 1 });
        });
        pedidos.forEach(function (p) {
            if (p.tipo === 'adicionar' && p.setor === c) {
                p.ruas.forEach(function (r) {
                    r.ll.forEach(function (pt) { bbPts.push(pt); });
                    linhas.push(fase === 'antes'
                        ? { ll: r.ll, cor: '#555', larg: 4, traco: [8, 10], halo: true, ordem: 2 }
                        : { ll: r.ll, cor: p.cls === 'manual' ? COR_CLASS.manual : COR_CLASS.coleta, larg: 6, halo: true, ordem: 2 });
                });
            }
            if (p.tipo === 'mover' && p.destino === c) {
                p.fids.forEach(function (f) {
                    var s = D.segs[f];
                    if (!s) return;
                    s.ll.forEach(function (pt) { bbPts.push(pt); });
                    if (s.shared.indexOf(c) >= 0) return;
                    linhas.push(fase === 'antes'
                        ? { ll: s.ll, cor: '#555', larg: 4, traco: [8, 10], halo: true, ordem: 2 }
                        : { ll: s.ll, cor: COR_CLASS[s.cls], larg: 6, halo: true, ordem: 2 });
                });
            }
        });
        if (foco && foco.length) bbPts = foco;
        if (!bbPts.length) bbPts = [[-8.28, -35.03], [-8.29, -35.04]];

        var a = Infinity, b = Infinity, cc = -Infinity, d = -Infinity;
        bbPts.forEach(function (p) {
            if (p[0] < a) a = p[0]; if (p[0] > cc) cc = p[0];
            if (p[1] < b) b = p[1]; if (p[1] > d) d = p[1];
        });
        if (foco) {
            var mLat = (a + cc) / 2, minLat = 0.0012, minLon = 0.0012 / Math.cos(mLat * Math.PI / 180);
            if (cc - a < minLat) { a = mLat - minLat; cc = mLat + minLat; }
            var mLon = (b + d) / 2;
            if (d - b < minLon) { b = mLon - minLon; d = mLon + minLon; }
        }
        var x0 = lon2x(b, 0), x1 = lon2x(d, 0), y0 = lat2y(cc, 0), y1 = lat2y(a, 0);
        var dx = Math.max(x1 - x0, 1e-7), dy = Math.max(y1 - y0, 1e-7);
        var S = Math.min(W * (foco ? 0.62 : 0.88) / dx, H * (foco ? 0.62 : 0.88) / dy);
        var fz = Math.log(S) / Math.LN2;
        if (fz > 18.5) { fz = 18.5; S = Math.pow(2, fz); }
        var cx = (x0 + x1) / 2 * S, cy = (y0 + y1) / 2 * S, ox = cx - W / 2, oy = cy - H / 2;
        function P(pt) { return [X + lon2x(pt[1], 0) * S - ox, Y + lat2y(pt[0], 0) * S - oy]; }

        ctx.save();
        ctx.beginPath(); ctx.rect(X, Y, W, H); ctx.clip();
        ctx.fillStyle = '#f2f2ef'; ctx.fillRect(X, Y, W, H);

        var zt = Math.max(1, Math.min(FUNDO_ZOOM_MAX, Math.round(fz)));
        var k = Math.pow(2, fz - zt), tam = 256 * k;
        var esq = ox / k, topo = oy / k;
        var tx0 = Math.floor(esq / 256), tx1 = Math.floor((esq + W / k) / 256), ty0 = Math.floor(topo / 256), ty1 = Math.floor((topo + H / k) / 256);
        var jobs = [];
        for (var tx = tx0; tx <= tx1; tx++) {
            for (var ty = ty0; ty <= ty1; ty++) {
                (function (tx, ty) {
                    var fontes = FUNDOS.slice(fundoInicio).map(function (fn) { return fn(zt, tx, ty); });
                    var tenta = function (i) {
                        if (i >= fontes.length) return Promise.resolve(null);
                        return carregarImg(fontes[i], 6500).then(function (im) { return im || tenta(i + 1); });
                    };
                    jobs.push(tenta(0).then(function (im) { return { im: im, tx: tx, ty: ty }; }));
                })(tx, ty);
            }
        }

        return Promise.all(jobs).then(function (tiles) {
            tiles.forEach(function (t) {
                if (!t.im) return;
                try { ctx.drawImage(t.im, X + t.tx * tam - ox, Y + t.ty * tam - oy, tam + 0.6, tam + 0.6); } catch (e) {}
            });
            ctx.fillStyle = 'rgba(255,255,255,0.38)'; ctx.fillRect(X, Y, W, H);

            ctx.lineCap = 'round'; ctx.lineJoin = 'round';
            ctx.strokeStyle = 'rgba(90,90,90,0.35)'; ctx.lineWidth = 2.5; ctx.setLineDash([]);
            var latMin = a - (cc - a) * 0.2, latMax = cc + (cc - a) * 0.2, lonMin = b - (d - b) * 0.2, lonMax = d + (d - b) * 0.2;
            D.lista.forEach(function (s) {
                if (s.code === c) return;
                var m = s.meio;
                if (m[0] < latMin || m[0] > latMax || m[1] < lonMin || m[1] > lonMax) return;
                ctx.beginPath();
                s.ll.forEach(function (pt, i) { var q = P(pt); if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
                ctx.stroke();
            });

            ctx.setLineDash([]);
            st.geoms.forEach(function (g) {
                var polys = g.type === 'Polygon' ? [g.coordinates] : (g.type === 'MultiPolygon' ? g.coordinates : []);
                polys.forEach(function (rings) {
                    ctx.beginPath();
                    rings.forEach(function (r) {
                        r.forEach(function (c2, i) { var q = P([c2[1], c2[0]]); if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
                        ctx.closePath();
                    });
                    ctx.fillStyle = hexA(st.cor, 0.08); ctx.fill('evenodd');
                    ctx.strokeStyle = hexA(st.cor, 0.7); ctx.lineWidth = 2.5; ctx.stroke();
                });
            });

            function tracar(l, cor, larg, traco) {
                ctx.beginPath();
                l.ll.forEach(function (pt, i) { var q = P(pt); if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
                ctx.strokeStyle = cor; ctx.lineWidth = larg; ctx.setLineDash(traco || []); ctx.stroke();
            }
            linhas.sort(function (p, q) { return p.ordem - q.ordem; });
            linhas.forEach(function (l) { if (l.halo) tracar(l, 'rgba(255,214,0,0.9)', 22, null); });
            linhas.forEach(function (l) { if (l.halo) tracar(l, '#ffffff', l.larg + 5, null); });
            linhas.forEach(function (l) { tracar(l, l.cor, l.larg, l.traco); });
            ctx.setLineDash([]);

            var latC = (a + cc) / 2, mpp = 156543.03392 * Math.cos(latC * Math.PI / 180) / S;
            var alvo = W * 0.18 * mpp, nice = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000], Lsc = nice[0];
            nice.forEach(function (n) { if (n <= alvo) Lsc = n; });
            var px = Lsc / mpp, bx = X + 24, by = Y + H - 30;
            ctx.fillStyle = 'rgba(255,255,255,0.85)'; ctx.fillRect(bx - 10, by - 40, px + 120, 56);
            ctx.fillStyle = '#222'; ctx.fillRect(bx, by - 6, px, 8);
            ctx.font = '600 22px ' + FONTE; ctx.fillText(Lsc >= 1000 ? (Lsc / 1000) + ' km' : Lsc + ' m', bx + px + 12, by + 2);
            ctx.font = '18px ' + FONTE; ctx.fillStyle = 'rgba(0,0,0,.6)';
            var att = 'Mapa: Esri · dados © OpenStreetMap';
            ctx.fillText(att, X + W - ctx.measureText(att).width - 12, Y + H - 12);
            ctx.restore();
            ctx.strokeStyle = '#b8c2bb'; ctx.lineWidth = 2; ctx.strokeRect(X, Y, W, H);
            return { P: P, X: X, Y: Y, W: W, H: H };
        });
    }

    function pontosPedido(p) {
        var pts = [];
        linhasDoPedido(p).forEach(function (ll) { ll.forEach(function (q) { pts.push(q); }); });
        return pts;
    }
    function numeroNoSetor(p, c) { return pedidosDoSetor(c).indexOf(p) + 1; }
    function circularNoMapaPDF(ctx, M, lista, c) {
        ctx.save(); ctx.beginPath(); ctx.rect(M.X, M.Y, M.W, M.H); ctx.clip();
        lista.forEach(function (p) {
            var pts = pontosPedido(p).map(M.P), a = Infinity, b = Infinity, cc = -Infinity, d = -Infinity;
            if (!pts.length) return;
            pts.forEach(function (q) { if (q[0] < a) a = q[0]; if (q[0] > cc) cc = q[0]; if (q[1] < b) b = q[1]; if (q[1] > d) d = q[1]; });
            var cx = (a + cc) / 2, cy = (b + d) / 2, r = Math.max(Math.sqrt((cc - a) * (cc - a) + (d - b) * (d - b)) / 2 + 22, 34);
            ctx.setLineDash([14, 10]); ctx.lineWidth = 5; ctx.strokeStyle = '#e00000';
            ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
            var bx = cx + r * 0.72, by = cy - r * 0.72;
            ctx.fillStyle = '#e00000'; ctx.beginPath(); ctx.arc(bx, by, 25, 0, Math.PI * 2); ctx.fill();
            ctx.strokeStyle = '#fff'; ctx.lineWidth = 4; ctx.stroke();
            ctx.fillStyle = '#fff'; ctx.font = '700 28px ' + FONTE;
            var t = String(numeroNoSetor(p, c));
            ctx.fillText(t, bx - ctx.measureText(t).width / 2, by + 10);
        });
        ctx.restore();
    }
    function etiquetaMapa(ctx, M, txt, cor) {
        ctx.font = '700 26px ' + FONTE;
        var w = ctx.measureText(txt).width + 32;
        ctx.fillStyle = cor; ctx.fillRect(M.X + 14, M.Y + 14, w, 46);
        ctx.fillStyle = '#fff'; ctx.fillText(txt, M.X + 30, M.Y + 46);
    }

    function quebrar(ctx, txt, larg) {
        var out = [];
        String(txt == null ? '' : txt).split('\n').forEach(function (par) {
            var pal = par.split(' '), lin = '';
            pal.forEach(function (w) {
                var t = lin ? lin + ' ' + w : w;
                if (ctx.measureText(t).width > larg && lin) { out.push(lin); lin = w; }
                else lin = t;
                while (ctx.measureText(lin).width > larg && lin.length > 4) {
                    var n = lin.length;
                    while (n > 1 && ctx.measureText(lin.slice(0, n)).width > larg) n--;
                    out.push(lin.slice(0, n));
                    lin = lin.slice(n);
                }
            });
            out.push(lin);
        });
        return out;
    }
    function medirLinhas(ctx, cols, linhas, fs) {
        ctx.font = fs + 'px ' + FONTE;
        return linhas.map(function (r) {
            var maxL = 1;
            var cel = r.celulas.map(function (t, i) {
                var ls = quebrar(ctx, t, cols[i].w - 24);
                if (ls.length > maxL) maxL = ls.length;
                return ls;
            });
            return { r: r, cel: cel, h: Math.max(fs * 1.45 * maxL + 20, fs * 2) };
        });
    }
    function desenharTabela(ctx, x, y, cols, medidas, fs) {
        var hh = fs * 2.1;
        ctx.fillStyle = '#e9f2ec'; ctx.fillRect(x, y, cols.reduce(function (a, c) { return a + c.w; }, 0), hh);
        ctx.fillStyle = '#006331'; ctx.font = '700 ' + fs + 'px ' + FONTE;
        var cx = x;
        cols.forEach(function (c) { ctx.fillText(c.t.toUpperCase(), cx + 12, y + hh * 0.66); cx += c.w; });
        y += hh;
        medidas.forEach(function (m, ri) {
            var tot = cols.reduce(function (a, c) { return a + c.w; }, 0);
            if (ri % 2) { ctx.fillStyle = '#fafbf9'; ctx.fillRect(x, y, tot, m.h); }
            ctx.strokeStyle = '#e1e6e2'; ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(x, y + m.h); ctx.lineTo(x + tot, y + m.h); ctx.stroke();
            if (m.r.cor) { ctx.fillStyle = m.r.cor; ctx.fillRect(x + (cols[0].t === 'Nº' ? cols[0].w : 0), y + 6, 9, m.h - 12); }
            var cx2 = x;
            m.cel.forEach(function (ls, i) {
                var num = cols[0].t === 'Nº', destaque = num ? i <= 1 : i === 0, comCor = m.r.cor && (num ? i === 1 : i === 0);
                ctx.fillStyle = '#1d2b22';
                ctx.font = (destaque && m.r.negrito0 ? '700 ' : '') + fs + 'px ' + FONTE;
                ls.forEach(function (l, j) { ctx.fillText(l, cx2 + (comCor ? 22 : 12), y + fs * 1.25 + j * fs * 1.45 + 4); });
                cx2 += cols[i].w;
            });
            y += m.h;
        });
        return y;
    }
    function paginar(medidas, primeiraAltura, demaisAltura, cab) {
        var pags = [[]], disp = primeiraAltura - cab, uso = 0;
        medidas.forEach(function (m) {
            if (uso + m.h > disp && pags[pags.length - 1].length) {
                pags.push([]);
                disp = demaisAltura - cab;
                uso = 0;
            }
            pags[pags.length - 1].push(m);
            uso += m.h;
        });
        return pags;
    }
    function novaPagina() {
        var cv = document.createElement('canvas'); cv.width = PW; cv.height = PH;
        var ctx = cv.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, PW, PH);
        ctx.textBaseline = 'alphabetic';
        return { cv: cv, ctx: ctx };
    }
    function faixaTopo(ctx, titulo, sub) {
        ctx.fillStyle = '#038242'; ctx.fillRect(0, 0, PW, 130);
        ctx.fillStyle = '#C3CD85'; ctx.fillRect(0, 130, PW, 8);
        ctx.fillStyle = '#fff'; ctx.font = '700 50px ' + FONTE; ctx.fillText(titulo, MG, 82);
        if (sub) { ctx.font = '26px ' + FONTE; var w = ctx.measureText(sub).width; ctx.fillText(sub, PW - MG - w, 80); }
    }
    function rodape(ctx, n, total, cab) {
        ctx.fillStyle = '#8a968e'; ctx.font = '22px ' + FONTE;
        ctx.fillText(cab, MG, PH - 40);
        var t = 'Página ' + n + ' de ' + total;
        ctx.fillText(t, PW - MG - ctx.measureText(t).width, PH - 40);
    }
    function legendaTiposPDF(ctx, x, y, tipos) {
        ctx.font = '24px ' + FONTE;
        var itens = [[COR_CLASS.coleta, 'Coleta (caminhão)', null], [COR_CLASS.manual, 'Coleta manual', null]];
        if (tipos.trajeto) itens.push([ACOES.trajeto.cor, 'Somente trajeto', [18, 12]]);
        if (tipos.temp) itens.push([ACOES.temp.cor, 'Interditado temporariamente', [18, 12]]);
        if (tipos.mover) itens.push(['#d63031', 'Sai deste setor', [6, 12]]);
        if (tipos.entra) itens.push(['#555', 'Ainda fora do setor (hoje)', [8, 10]]);
        itens.push(['halo', 'Trecho com alteração', null]);
        itens.push(['ctx', 'Ruas de outros setores', null]);
        var cx = x, limite = PW - MG;
        itens.forEach(function (it) {
            ctx.lineCap = 'round';
            if (cx + 66 + ctx.measureText(it[1]).width > limite) { cx = x; y += 46; }
            if (it[0] === 'halo') {
                ctx.strokeStyle = 'rgba(255,214,0,.9)'; ctx.lineWidth = 20; ctx.beginPath(); ctx.moveTo(cx + 8, y); ctx.lineTo(cx + 52, y); ctx.stroke();
                ctx.strokeStyle = '#fff'; ctx.lineWidth = 10; ctx.stroke();
                ctx.strokeStyle = '#888'; ctx.lineWidth = 5; ctx.stroke();
            } else if (it[0] === 'ctx') {
                ctx.strokeStyle = 'rgba(90,90,90,.45)'; ctx.lineWidth = 3; ctx.setLineDash([]); ctx.beginPath(); ctx.moveTo(cx + 8, y); ctx.lineTo(cx + 52, y); ctx.stroke();
            } else {
                ctx.strokeStyle = it[0]; ctx.lineWidth = 7; ctx.setLineDash(it[2] || []); ctx.beginPath(); ctx.moveTo(cx + 8, y); ctx.lineTo(cx + 52, y); ctx.stroke(); ctx.setLineDash([]);
            }
            ctx.fillStyle = '#333'; ctx.fillText(it[1], cx + 66, y + 8);
            cx += 66 + ctx.measureText(it[1]).width + 44;
        });
    }
    function linhasPedidoSetor(c) {
        var out = [];
        pedidosDoSetor(c).forEach(function (p) {
            var d = descrPedido(p), a = ACOES[p.tipo], tipo = a.nome, det = d.det;
            if (p.tipo === 'mover' && p.destino === c) { tipo = 'Recebe trechos'; det = 'Vêm do setor ' + p.setor; }
            else if (p.tipo === 'mover') { det = 'Vão para o setor ' + p.destino; }
            out.push({
                cor: p.tipo === 'mover' && p.destino === c ? '#8e44ad' : a.cor,
                negrito0: true,
                celulas: [String(numeroNoSetor(p, c)), tipo, d.nomes.join(', '), d.n + ' trecho(s)\n' + fmtM(d.comp), det || '—', p.obs || '—']
            });
        });
        return out;
    }
    function quaisTipos(c) {
        var t = {};
        pedidosDoSetor(c).forEach(function (p) {
            if (p.tipo === 'mover' && p.destino === c) t.entra = 1;
            else t[p.tipo] = 1;
            if (p.tipo === 'adicionar') t.entra = 1;
        });
        return t;
    }
    function canvasParaJPEG(cv) {
        return new Promise(function (res) {
            cv.toBlob(function (b) {
                b.arrayBuffer().then(function (ab) { res(new Uint8Array(ab)); });
            }, 'image/jpeg', 0.86);
        });
    }

    function desenharCapaPDF(ctx, nome, dataHoje, setores) {
        ctx.fillStyle = '#038242'; ctx.fillRect(0, 0, PW, 24);
        ctx.fillStyle = '#C3CD85'; ctx.fillRect(0, 24, PW, 8);
        return carregarImg(LOGO_URL, 4000).then(function (im) {
            if (im) { var h = 135, w = im.width * h / im.height; ctx.drawImage(im, MG, 80, w, h); }
            ctx.fillStyle = '#006331'; ctx.font = '700 62px ' + FONTE;
            var tt = 'Solicitação de Correção de Rotas'; ctx.fillText(tt, PW - MG - ctx.measureText(tt).width, 150);
            ctx.fillStyle = '#5d6b62'; ctx.font = '30px ' + FONTE;
            var st2 = 'Contrato ' + D.contrato; ctx.fillText(st2, PW - MG - ctx.measureText(st2).width, 205);
            ctx.fillStyle = '#e1e6e2'; ctx.fillRect(MG, 270, PW - 2 * MG, 3);

            var info = [
                ['Contrato', D.contrato],
                ['Solicitante', nome],
                ['Data da solicitação', dataHoje],
                ['Mapa usado', D.titulo],
                ['Total de alterações', pedidos.length + ' em ' + setores.length + ' setor(es)']
            ];
            var og = ($('crObsGeral').value || '').trim();
            if (og) info.push(['Observações gerais', og]);
            var y = 340;
            info.forEach(function (r) {
                ctx.font = '600 26px ' + FONTE; ctx.fillStyle = '#5d6b62'; ctx.fillText(r[0].toUpperCase(), MG, y);
                ctx.font = '32px ' + FONTE; ctx.fillStyle = '#1d2b22';
                quebrar(ctx, r[1], 1700 - MG - 520 - 60).forEach(function (l, i) { ctx.fillText(l, MG + 520, y + i * 40); });
                y += 22 + 40 * quebrar(ctx, r[1], 1700 - MG - 520 - 60).length;
            });

            var usados = {}; pedidos.forEach(function (p) { usados[p.tipo] = 1; });
            var yx = 340; ctx.font = '600 26px ' + FONTE; ctx.fillStyle = '#5d6b62'; ctx.fillText('TIPOS DE ALTERAÇÃO', 1760, yx);
            Object.keys(ACOES).forEach(function (k) {
                if (!usados[k]) return;
                yx += 50;
                ctx.fillStyle = ACOES[k].cor; ctx.fillRect(1760, yx - 24, 12, 30);
                ctx.fillStyle = '#1d2b22'; ctx.font = '30px ' + FONTE; ctx.fillText(ACOES[k].nome, 1790, yx);
            });

            var cols = [{ t: 'Setor', w: 300 }, { t: 'Agenda', w: 460 }, { t: 'Turno', w: 220 }, { t: 'Alterações', w: 860 }, { t: 'Trechos', w: 150 }, { t: 'Extensão', w: PW - 2 * MG - 1990 }];
            var linhas = setores.map(function (c) {
                var st = D.setores[c] || {}, cnt = {}, n = 0, comp = 0;
                pedidosDoSetor(c).forEach(function (p) {
                    var nm = (p.tipo === 'mover' && p.destino === c) ? 'Recebe trechos' : ACOES[p.tipo].nome;
                    cnt[nm] = (cnt[nm] || 0) + 1;
                    var d = descrPedido(p); n += d.n; comp += d.comp;
                });
                return {
                    negrito0: true,
                    celulas: [c, st.agenda || '—', st.turno || '—', Object.keys(cnt).map(function (k) { return k + (cnt[k] > 1 ? ' (' + cnt[k] + ')' : ''); }).join(', '), String(n), fmtM(comp)]
                };
            });
            var med = medirLinhas(ctx, cols, linhas, 26), yT = Math.max(y, yx) + 50;
            ctx.font = '700 30px ' + FONTE; ctx.fillStyle = '#006331'; ctx.fillText('Resumo por setor', MG, yT); yT += 24;
            var caber = [], soma = 0, lim = PH - 220 - yT;
            med.forEach(function (m) { if (soma + m.h < lim) { caber.push(m); soma += m.h; } });
            var yFim = desenharTabela(ctx, MG, yT, cols, caber, 26);
            if (caber.length < med.length) {
                ctx.font = 'italic 24px ' + FONTE; ctx.fillStyle = '#5d6b62';
                ctx.fillText('… e mais ' + (med.length - caber.length) + ' setor(es) nas páginas seguintes.', MG, yFim + 40);
            }
            ctx.font = '24px ' + FONTE; ctx.fillStyle = '#5d6b62';
            ctx.fillText('Nas páginas seguintes, cada setor mostra o mapa “como está hoje” e “como deve ficar”. Cada alteração está circulada e numerada.', MG, PH - 140);
            ctx.fillText('O arquivo técnico (.geojson) com a localização exata de cada trecho está anexado a este PDF e disponível no Painel Central.', MG, PH - 100);
        });
    }

    function montarPDFBinario(jpgs, meta) {
        var enc = new TextEncoder(), partes = [], tam = 0, offs = [];
        function put(x) { var b = typeof x === 'string' ? enc.encode(x) : x; partes.push(b); tam += b.length; }
        function obj(n, corpo) { offs[n] = tam; put(n + ' 0 obj\n'); corpo(); put('\nendobj\n'); }
        function utf16(s) { var h = 'FEFF'; for (var i = 0; i < s.length; i++) h += s.charCodeAt(i).toString(16).padStart(4, '0'); return '<' + h.toUpperCase() + '>'; }
        function asc(s) { return '(' + s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^\x20-\x7e]/g, '_').replace(/([()\\])/g, '\\$1') + ')'; }
        var WPT = 841.89, HPT = 595.28;
        var n = jpgs.length, base = 6;
        put('%PDF-1.7\n'); put(new Uint8Array([0x25, 0xE2, 0xE3, 0xCF, 0xD3, 0x0A]));
        obj(1, function () { put('<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [' + utf16(meta.anexo.nome) + ' 5 0 R] >> >> >>'); });
        var kids = []; for (var i = 0; i < n; i++) kids.push((base + i * 3) + ' 0 R');
        obj(2, function () { put('<< /Type /Pages /Count ' + n + ' /Kids [' + kids.join(' ') + '] >>'); });
        var d = new Date(), ds = 'D:' + d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0') + String(d.getHours()).padStart(2, '0') + String(d.getMinutes()).padStart(2, '0') + '00';
        obj(3, function () { put('<< /Title ' + utf16(meta.titulo) + ' /Author ' + utf16(meta.autor) + ' /Creator ' + utf16('Correção de Rotas · Locar') + ' /CreationDate (' + ds + ') >>'); });
        obj(4, function () { put('<< /Type /EmbeddedFile /Subtype /application#2Fgeo+json /Length ' + meta.anexo.bytes.length + ' >>\nstream\n'); put(meta.anexo.bytes); put('\nendstream'); });
        obj(5, function () { put('<< /Type /Filespec /F ' + asc(meta.anexo.nome) + ' /UF ' + utf16(meta.anexo.nome) + ' /Desc ' + utf16('Arquivo técnico com os trechos alterados (WGS 84)') + ' /EF << /F 4 0 R >> >>'); });
        jpgs.forEach(function (j, idx) {
            var pg = base + idx * 3, ct = pg + 1, im = pg + 2;
            var cont = 'q ' + WPT.toFixed(2) + ' 0 0 ' + HPT.toFixed(2) + ' 0 0 cm /Im0 Do Q';
            obj(pg, function () { put('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ' + WPT.toFixed(2) + ' ' + HPT.toFixed(2) + '] /Resources << /XObject << /Im0 ' + im + ' 0 R >> >> /Contents ' + ct + ' 0 R >>'); });
            obj(ct, function () { put('<< /Length ' + cont.length + ' >>\nstream\n' + cont + '\nendstream'); });
            obj(im, function () { put('<< /Type /XObject /Subtype /Image /Width ' + PW + ' /Height ' + PH + ' /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ' + j.length + ' >>\nstream\n'); put(j); put('\nendstream'); });
        });
        var total = base + n * 3, xref = tam;
        var x = 'xref\n0 ' + total + '\n0000000000 65535 f \n';
        for (var k = 1; k < total; k++) x += String(offs[k]).padStart(10, '0') + ' 00000 n \n';
        put(x + 'trailer\n<< /Size ' + total + ' /Root 1 0 R /Info 3 0 R >>\nstartxref\n' + xref + '\n%%EOF\n');
        return new Blob(partes, { type: 'application/pdf' });
    }

    function mostrarProgressoPDF(txt) {
        if (txt === null) { fecharCrModal(); return; }
        abrirCrModal('<h2>⏳ Gerando PDF e Enviando Solicitação…</h2><div class="cr-dica" style="font-size:13.5px">' + esc(txt) + '</div>', false);
    }

    function gerarEnviarEBaixarPDF() {
        var nome = ($('crNomeSolic').value || '').trim();
        if (!nome) {
            crToast('Preencha o seu nome antes de finalizar e gerar o PDF.');
            $('crNomeSolic').focus();
            return;
        }
        if (!pedidos.length || !D) return;
        salvarRascunho();

        var setores = setoresAfetados();
        var dataHoje = new Date().toLocaleDateString('pt-BR') + ' ' + new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        var cab = 'Solicitação de Correção de Rotas · ' + D.contrato + ' · ' + nome + ' · ' + dataHoje;
        var medidor = novaPagina().ctx;

        var plano = [{ tipo: 'capa' }];
        var COLS_S = [{ t: 'Nº', w: 80 }, { t: 'Alteração', w: 300 }, { t: 'Ruas', w: 560 }, { t: 'Extensão', w: 190 }, { t: 'Detalhe', w: 420 }, { t: 'Motivo / observação', w: PW - 2 * MG - 1550 }];
        setores.forEach(function (c) {
            var med = medirLinhas(medidor, COLS_S, linhasPedidoSetor(c), 24);
            var pags = paginar(med, PH - 1150 - 90, PH - 200 - 90, 24 * 2.1);
            pags.forEach(function (rows, i) { plano.push({ tipo: i ? 'setorCont' : 'setor', c: c, rows: rows }); });
            var lst = pedidosDoSetor(c);
            for (var q = 0; q < lst.length; q += 2) plano.push({ tipo: 'detalhe', c: c, itens: lst.slice(q, q + 2) });
        });

        var COLS_A = [{ t: 'Setor · nº', w: 190 }, { t: 'Alteração', w: 260 }, { t: 'Rua', w: 520 }, { t: 'Identificação', w: 520 }, { t: 'Coordenada (meio)', w: 330 }, { t: 'Extensão', w: 150 }, { t: 'Destino / período', w: PW - 2 * MG - 1970 }];
        var linA = [];
        pedidos.forEach(function (p) {
            var extra = p.tipo === 'mover' ? 'para ' + p.destino : (p.tipo === 'temp' ? fmtData(p.de) + ' a ' + fmtData(p.ate) : (p.tipo === 'adicionar' ? (p.cls === 'manual' ? 'coleta manual' : 'coleta normal') : ''));
            var sn = p.setor + ' · ' + numeroNoSetor(p, p.setor);
            if (p.tipo === 'adicionar') {
                p.ruas.forEach(function (r) {
                    linA.push({ celulas: [sn, ACOES[p.tipo].nome, r.nome || 'Rua sem nome', 'Malha Viária (' + (r.via || 'via') + ')', r.meio[0].toFixed(6) + ', ' + r.meio[1].toFixed(6), fmtM(r.len), extra] });
                });
            } else {
                p.fids.forEach(function (f) {
                    var s = D.segs[f];
                    if (!s) return;
                    linA.push({ celulas: [sn, ACOES[p.tipo].nome, s.rua || 'Rua sem nome', 'trecho ' + s.origId + ' (' + s.rota + ')', s.meio[0].toFixed(6) + ', ' + s.meio[1].toFixed(6), fmtM(s.len), extra] });
                });
            }
        });
        var medA = medirLinhas(medidor, COLS_A, linA, 20);
        paginar(medA, PH - 260 - 90, PH - 260 - 90, 20 * 2.1).forEach(function (rows, i) { plano.push({ tipo: 'anexo', rows: rows, i: i }); });
        var total = plano.length;

        var jpgs = [], idx = 0;
        function proxima() {
            if (idx >= plano.length) return finalizar();
            var pg = plano[idx], n = idx + 1; idx++;
            mostrarProgressoPDF('Desenhando página ' + n + ' de ' + total + (pg.c ? ' · setor ' + pg.c : '') + '…');
            var P = novaPagina(), ctx = P.ctx, prom = Promise.resolve();

            if (pg.tipo === 'capa') prom = desenharCapaPDF(ctx, nome, dataHoje, setores);
            if (pg.tipo === 'setor' || pg.tipo === 'setorCont') {
                var st = D.setores[pg.c] || {};
                faixaTopo(ctx, 'Setor ' + pg.c + (pg.tipo === 'setorCont' ? ' (continuação)' : ''), (st.agenda ? st.agenda + ' · ' : '') + (st.turno ? st.turno + ' · ' : '') + D.contrato);
                var yTab = 200;
                if (pg.tipo === 'setor') {
                    var wm = (PW - 2 * MG - 40) / 2, hm = 800, ym = 215;
                    ctx.font = '700 30px ' + FONTE; ctx.fillStyle = '#5d6b62'; ctx.fillText('COMO ESTÁ HOJE', MG, ym - 18);
                    ctx.fillStyle = '#038242'; ctx.fillText('COMO DEVE FICAR', MG + wm + 40, ym - 18);
                    var lst = pedidosDoSetor(pg.c);
                    prom = desenharMapaPDF(ctx, MG, ym, wm, hm, pg.c, 'antes')
                        .then(function (M) { circularNoMapaPDF(ctx, M, lst, pg.c); return desenharMapaPDF(ctx, MG + wm + 40, ym, wm, hm, pg.c, 'depois'); })
                        .then(function (M) { circularNoMapaPDF(ctx, M, lst, pg.c); legendaTiposPDF(ctx, MG, ym + hm + 40, quaisTipos(pg.c)); });
                    yTab = 1150;
                }
                var cRows = pg.rows;
                prom = prom.then(function () { desenharTabela(ctx, MG, yTab, COLS_S, cRows, 24); });
            }
            if (pg.tipo === 'detalhe') {
                var st2 = D.setores[pg.c] || {};
                faixaTopo(ctx, 'Setor ' + pg.c + ' · detalhe das alterações', (st2.agenda ? st2.agenda + ' · ' : '') + D.contrato);
                var wd = (PW - 2 * MG - 40) / 2, hd = 540;
                pg.itens.forEach(function (p, i) {
                    prom = prom.then(function () {
                        var y0 = 170 + i * 660, num = numeroNoSetor(p, pg.c), a = ACOES[p.tipo], ds = descrPedido(p);
                        var nomeT = (p.tipo === 'mover' && p.destino === pg.c) ? 'Recebe trechos do setor ' + p.setor : a.nome + (p.tipo === 'mover' ? ' → ' + p.destino : '');
                        ctx.fillStyle = '#e00000'; ctx.beginPath(); ctx.arc(MG + 26, y0 + 30, 26, 0, Math.PI * 2); ctx.fill();
                        ctx.fillStyle = '#fff'; ctx.font = '700 28px ' + FONTE; var tn = String(num); ctx.fillText(tn, MG + 26 - ctx.measureText(tn).width / 2, y0 + 40);
                        ctx.fillStyle = (p.tipo === 'mover' && p.destino === pg.c) ? '#8e44ad' : a.cor; ctx.fillRect(MG + 66, y0 + 6, 10, 50);
                        ctx.fillStyle = '#1d2b22'; ctx.font = '700 32px ' + FONTE;
                        var t1 = nomeT + ' · ' + resumoRuas(ds.nomes, 3) + ' · ' + fmtM(ds.comp);
                        ctx.fillText(quebrar(ctx, t1, PW - 2 * MG - 100)[0], MG + 92, y0 + 30);
                        ctx.fillStyle = '#5d6b62'; ctx.font = '24px ' + FONTE;
                        var t2 = [(p.tipo === 'mover' && p.destino === pg.c) ? '' : ds.det, p.obs ? 'Motivo: ' + p.obs : ''].filter(Boolean).join('   ·   ');
                        if (t2) ctx.fillText(quebrar(ctx, t2, PW - 2 * MG - 100)[0], MG + 92, y0 + 62);
                        var foco = pontosPedido(p), ym2 = y0 + 86;
                        return desenharMapaPDF(ctx, MG, ym2, wd, hd, pg.c, 'antes', foco)
                            .then(function (M) {
                                circularNoMapaPDF(ctx, M, [p], pg.c);
                                etiquetaMapa(ctx, M, 'COMO ESTÁ HOJE', '#5d6b62');
                                return desenharMapaPDF(ctx, MG + wd + 40, ym2, wd, hd, pg.c, 'depois', foco);
                            })
                            .then(function (M) {
                                circularNoMapaPDF(ctx, M, [p], pg.c);
                                etiquetaMapa(ctx, M, 'COMO DEVE FICAR', '#038242');
                            });
                    });
                });
                prom = prom.then(function () {
                    var t = {};
                    pg.itens.forEach(function (p) { if (p.tipo === 'mover' && p.destino === pg.c) t.entra = 1; else t[p.tipo] = 1; if (p.tipo === 'adicionar') t.entra = 1; });
                    legendaTiposPDF(ctx, MG, 170 + pg.itens.length * 660 - 10, t);
                });
            }
            if (pg.tipo === 'anexo') {
                faixaTopo(ctx, 'Anexo técnico' + (pg.i ? ' (continuação)' : ''), 'Para a equipe de monitoramento Locar');
                ctx.font = '24px ' + FONTE; ctx.fillStyle = '#5d6b62';
                ctx.fillText('Identificação de cada trecho para localizar no QGIS. O mesmo conteúdo está no arquivo .geojson anexado a este PDF (WGS 84).', MG, 200);
                desenharTabela(ctx, MG, 240, COLS_A, pg.rows, 20);
            }
            return prom
                .then(function () { rodape(ctx, n, total, cab); return canvasParaJPEG(P.cv); })
                .then(function (j) { jpgs.push(j); return new Promise(function (r) { setTimeout(r, 10); }); })
                .then(proxima);
        }

        function finalizar() {
            mostrarProgressoPDF('Montando PDF com GeoJSON técnico e enviando para o Painel Central Locar…');
            var geoObj = montarGeoJSON();
            var geoBytes = new TextEncoder().encode(JSON.stringify(geoObj, null, 1));
            var baseName = nomeBaseArquivo();
            var pdfFilename = baseName + '.pdf';
            var geoFilename = baseName + '.geojson';

            var pdfBlob = montarPDFBinario(jpgs, {
                titulo: 'Solicitação de Correção de Rotas - ' + D.contrato,
                autor: nome,
                anexo: { nome: geoFilename, bytes: geoBytes }
            });

            // 1. Baixa o PDF imediatamente para o usuário
            baixarBlob(pdfBlob, pdfFilename);

            // 2. Envia automaticamente para o Painel Central Administrativo (Menu Principal)
            var sh = window.__SISTEMA_LC__ ? window.__SISTEMA_LC__.getSharedInfo() : {};
            var pedidosResumo = pedidos.map(function (p) {
                var ds = descrPedido(p);
                return {
                    id: p.id,
                    tipo: p.tipo,
                    acao_nome: ACOES[p.tipo].nome,
                    setor: p.setor,
                    destino: p.destino || null,
                    de: p.de || null,
                    ate: p.ate || null,
                    cls: p.cls || null,
                    obs: p.obs || '',
                    detalhe: ds.det || '',
                    ruas_nomes: ds.nomes,
                    trechos_qtd: ds.n,
                    extensao_m: Math.round(ds.comp * 10) / 10
                };
            });

            return blobParaBase64(pdfBlob).then(function (b64) {
                return fetch('/api/correcao-rotas/enviar', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        contrato_id: D.contratoId,
                        contrato_nome: D.contrato,
                        snapshot_id: sh.snapshotId || null,
                        token: sh.token || null,
                        periodo: D.periodoRaw,
                        data_ref: D.de + (D.ate && D.ate !== D.de ? ' a ' + D.ate : ''),
                        solicitante: nome,
                        observacoes_gerais: ($('crObsGeral').value || '').trim(),
                        pedidos: pedidosResumo,
                        geojson: geoObj,
                        pdf_base64: b64,
                        pdf_filename: pdfFilename
                    })
                }).then(function (r) { return r.json(); }).catch(function () { return { sucesso: false }; });
            }).then(function (respEnvio) {
                mostrarProgressoPDF(null);
                var envOk = respEnvio && respEnvio.sucesso;
                var protHtml = envOk
                    ? '<div class="cr-alerta" style="background:#ECFDF5;border-color:#6EE7B7;color:#065F46;font-size:13px;">' +
                      '✅ <b>Enviado automaticamente ao Painel Central Locar!</b><br>Protocolo de análise: <b>' + esc(respEnvio.protocolo) + '</b></div>'
                    : '<div class="cr-alerta">⚠️ O PDF foi salvo no seu computador, mas não foi possível confirmar o envio online agora. Envie o PDF baixado à equipe Locar.</div>';

                abrirCrModal(
                    '<h2>✅ Solicitação Concluída e PDF Gerado</h2>' +
                    protHtml +
                    '<p style="font-size:13.5px;line-height:1.5;margin:10px 0;">O arquivo <b>' + esc(pdfFilename) + '</b> foi baixado no seu dispositivo e já consta na Central de Análise da Locar.</p>' +
                    '<div class="resumo">Suas alterações continuam salvas como rascunho neste navegador caso queira revisar. Use “Limpar tudo” para iniciar uma nova solicitação do zero.</div>' +
                    '<div class="rodape">' +
                    '<button type="button" class="cr-btn-sec" id="crMLimpar">Limpar tudo</button>' +
                    '<button type="button" class="cr-btn-sec" id="crMBaixarGeo">Baixar .geojson</button>' +
                    '<button type="button" class="cr-btn-sec" id="crMDeNovo">Baixar PDF de novo</button>' +
                    '<button type="button" class="cr-btn" id="crMFecharFim">Fechar</button>' +
                    '</div>',
                    false
                );

                if (!sh.isSharedLink) atualizarBadgePendentesAdmin();

                $('crMFecharFim').onclick = fecharCrModal;
                $('crMDeNovo').onclick = function () { baixarBlob(pdfBlob, pdfFilename); };
                $('crMBaixarGeo').onclick = function () { baixarBlob(new Blob([geoBytes], { type: 'application/geo+json' }), geoFilename); };
                $('crMLimpar').onclick = function () {
                    if (!confirm('Apagar todas as alterações desta solicitação?')) return;
                    pedidos = [];
                    salvarRascunho();
                    desenharPedidosMapa();
                    renderPedidos();
                    renderListaSetores();
                    desenharLegenda();
                    fecharCrModal();
                };
            });
        }

        proxima().catch(function (e) {
            console.error(e);
            mostrarProgressoPDF(null);
            crToast('Erro ao gerar o PDF: ' + e.message);
        });
    }

    // ======================================================================
    // CONTROLE DE ABERTURA E FECHAMENTO DA ABA LATERAL
    // ======================================================================
    function alternarAbaCorrecaoRotas(forcarEstado) {
        var novoEstado = typeof forcarEstado === 'boolean' ? forcarEstado : !correcaoAtiva;
        var lc = window.__SISTEMA_LC__;
        if (!lc) return;

        if (novoEstado) {
            var audit = lc.getAudit();
            if (!audit || !audit.resumo) {
                crToast('Aguarde o carregamento do mapa antes de abrir a Correção de Rotas.');
                return;
            }
            if (String(audit.resumo.contrato_id || '').toLowerCase() === 'all') {
                crToast('Selecione uma cidade específica antes de abrir a Correção de Rotas.');
                return;
            }
            if (audit.resumo.sem_malha_viaria) {
                crToast('Esta cidade ainda não possui malha viária carregada.');
                return;
            }

            garantirCamadasNoMapa();
            if (camadaAdminPreview && getMap() && getMap().hasLayer(camadaAdminPreview)) {
                camadaAdminPreview.clearLayers();
                getMap().removeLayer(camadaAdminPreview);
            }
            correcaoAtiva = true;
            document.body.classList.add('cr-ativo');
            var btn = $('btnAbrirCorrecaoRotas');
            if (btn) {
                btn.classList.add('ativo');
                btn.innerHTML = '✕ Fechar Correção';
            }
            lc.suspenderMapaPadrao(true);
            if (construirDadosDoMapaAtual()) {
                iniciarPainelCorrecao();
            }
            setTimeout(function () { if (getMap()) getMap().invalidateSize(); }, 120);
        } else {
            correcaoAtiva = false;
            document.body.classList.remove('cr-ativo');
            var btn2 = $('btnAbrirCorrecaoRotas');
            if (btn2) {
                btn2.classList.remove('ativo');
                btn2.innerHTML = '✏️ Correção de Rotas';
            }
            limparCamadasCorrecao();
            lc.suspenderMapaPadrao(false);
            setTimeout(function () { if (getMap()) getMap().invalidateSize(); }, 120);
        }
    }

    // ======================================================================
    // PAINEL ADMINISTRATIVO NO MENU PRINCIPAL (RECEBIMENTO E ANÁLISE DE PDFS)
    // ======================================================================
    var solSelecionadaId = null;

    function atualizarBadgePendentesAdmin() {
        var sh = window.__SISTEMA_LC__ ? window.__SISTEMA_LC__.getSharedInfo() : {};
        if (sh.isSharedLink) return;
        var badge = $('badgeSolicPendentes');
        if (!badge) return;
        fetch('/api/correcao-rotas/listar')
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (j) {
                if (!j || !j.sucesso) return;
                var n = Number(j.pendentes_total || 0);
                badge.textContent = String(n);
                badge.style.display = n > 0 ? 'inline-block' : 'none';
            })
            .catch(function () {});
    }

    function abrirCentralSolicitacoesRotas() {
        var modal = $('modalAdminSolicitacoes');
        if (!modal) return;
        modal.style.display = 'flex';
        carregarListaSolicitacoesAdmin();
    }

    function carregarListaSolicitacoesAdmin() {
        var cid = ($('solFiltroCidade') && $('solFiltroCidade').value) || 'all';
        var st = ($('solFiltroStatus') && $('solFiltroStatus').value) || 'ALL';
        var box = $('solListaContainer');
        if (box) box.innerHTML = '<div style="padding:20px;text-align:center;color:#64748B;">Carregando solicitações…</div>';

        fetch('/api/correcao-rotas/listar?cidade=' + encodeURIComponent(cid) + '&status=' + encodeURIComponent(st))
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (!j || !j.sucesso) {
                    if (box) box.innerHTML = '<div style="padding:20px;color:#DC2626;">Erro ao carregar solicitações.</div>';
                    return;
                }
                var badge = $('badgeSolicPendentes');
                if (badge) {
                    var np = Number(j.pendentes_total || 0);
                    badge.textContent = String(np);
                    badge.style.display = np > 0 ? 'inline-block' : 'none';
                }
                var lista = j.solicitacoes || [];
                if (!lista.length) {
                    box.innerHTML = '<div style="padding:28px 16px;text-align:center;color:#64748B;font-size:0.84rem;">Nenhuma solicitação encontrada para este filtro.</div>';
                    $('solDetalheContainer').innerHTML = '<div style="margin:auto;text-align:center;color:#94A3B8;padding:40px;">Selecione uma solicitação à esquerda ou importe um PDF externo para analisar.</div>';
                    return;
                }
                var h = '';
                lista.forEach(function (it) {
                    var dtFmt = String(it.criado_em || '').replace('T', ' ').slice(0, 16);
                    var stLabel = { PENDENTE: 'Pendente', EM_ANALISE: 'Em Análise', APROVADA: 'Aprovada', CONCLUIDA: 'Concluída', REJEITADA: 'Rejeitada' }[it.status] || it.status;
                    h += '<div class="sol-card-item' + (solSelecionadaId === it.id ? ' ativo' : '') + '" data-id="' + esc(it.id) + '">' +
                         '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">' +
                         '<span style="font-weight:800;font-size:0.80rem;color:#0F172A;">' + esc(it.protocolo) + '</span>' +
                         '<span class="sol-status-pill sol-status-' + esc(it.status) + '">' + esc(stLabel) + '</span>' +
                         '</div>' +
                         '<div style="font-weight:700;font-size:0.83rem;color:#038242;">' + esc(it.contrato_nome) + '</div>' +
                         '<div style="font-size:0.76rem;color:#475569;margin-top:2px;">👤 ' + esc(it.solicitante) + ' · ' + esc(dtFmt) + '</div>' +
                         '<div style="font-size:0.73rem;color:#64748B;margin-top:3px;">📌 ' + it.qtd_alteracoes + ' alteração(ões) em ' + it.qtd_setores + ' setor(es)' +
                         (it.tamanho_kb ? ' · PDF ' + it.tamanho_kb + ' KB' : '') + '</div>' +
                         '</div>';
                });
                box.innerHTML = h;
                Array.prototype.forEach.call(box.querySelectorAll('.sol-card-item'), function (el) {
                    el.onclick = function () { selecionarSolicitacaoAdmin(el.dataset.id); };
                });
                var existeSel = lista.some(function (x) { return x.id === solSelecionadaId; });
                selecionarSolicitacaoAdmin(existeSel ? solSelecionadaId : lista[0].id);
            })
            .catch(function (e) {
                if (box) box.innerHTML = '<div style="padding:20px;color:#DC2626;">Falha de rede: ' + esc(e.message) + '</div>';
            });
    }

    function selecionarSolicitacaoAdmin(id) {
        solSelecionadaId = id;
        Array.prototype.forEach.call(document.querySelectorAll('#solListaContainer .sol-card-item'), function (el) {
            el.classList.toggle('ativo', el.dataset.id === id);
        });
        var detBox = $('solDetalheContainer');
        if (!detBox) return;
        detBox.innerHTML = '<div style="padding:24px;color:#64748B;">Carregando detalhes da solicitação…</div>';

        fetch('/api/correcao-rotas/detalhe?id=' + encodeURIComponent(id))
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (!j || !j.sucesso || !j.solicitacao) {
                    detBox.innerHTML = '<div style="padding:24px;color:#DC2626;">Não foi possível carregar os detalhes.</div>';
                    return;
                }
                var s = j.solicitacao;
                var dtFmt = String(s.criado_em || '').replace('T', ' ').slice(0, 19);
                var pdfUrl = '/api/correcao-rotas/pdf?id=' + encodeURIComponent(s.id);
                var pdfDlUrl = pdfUrl + '&download=1';
                var geoUrl = '/api/correcao-rotas/geojson?id=' + encodeURIComponent(s.id);

                var h = '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;border-bottom:1px solid #E2E8F0;padding-bottom:10px;">' +
                    '<div>' +
                    '<div style="font-size:1.02rem;font-weight:800;color:#0F172A;">' + esc(s.protocolo) + ' — ' + esc(s.contrato_nome) + '</div>' +
                    '<div style="font-size:0.80rem;color:#475569;margin-top:3px;">Solicitante: <b>' + esc(s.solicitante) + '</b> · Recebido em: <b>' + esc(dtFmt) + '</b>' +
                    (s.data_ref ? ' · Ref.: ' + esc(s.data_ref) : '') + '</div>' +
                    (s.observacoes_gerais ? '<div style="margin-top:6px;background:#FFFBEB;border:1px solid #FDE68A;padding:6px 10px;border-radius:6px;font-size:0.78rem;color:#92400E;"><b>Obs. Gerais:</b> ' + esc(s.observacoes_gerais) + '</div>' : '') +
                    '</div>' +
                    '<div style="display:flex;gap:6px;flex-wrap:wrap;">' +
                    (s.tem_pdf ? '<button type="button" class="cr-btn" id="btnSolTogglePdf" style="padding:7px 11px;font-size:0.78rem;">👁️ Ler PDF na Tela</button>' : '') +
                    (s.tem_pdf ? '<a class="cr-btn-sec" href="' + pdfUrl + '" target="_blank" style="text-decoration:none;display:inline-flex;align-items:center;">↗ Nova Aba</a>' : '') +
                    (s.tem_pdf ? '<a class="cr-btn-sec" href="' + pdfDlUrl + '" style="text-decoration:none;display:inline-flex;align-items:center;">⬇ Baixar PDF</a>' : '') +
                    (s.tem_geojson ? '<a class="cr-btn-sec" href="' + geoUrl + '" style="text-decoration:none;display:inline-flex;align-items:center;border-color:#038242;color:#006331;">🌍 Baixar .geojson (QGIS)</a>' : '') +
                    (s.tem_geojson ? '<button type="button" class="cr-btn-sec" id="btnSolVerNoMapa">🗺️ Ver no Mapa</button>' : '') +
                    '</div></div>';

                if (s.tem_pdf) {
                    h += '<div id="solPdfViewerWrap" style="display:block;">' +
                         '<iframe class="sol-pdf-iframe" src="' + pdfUrl + '"></iframe>' +
                         '</div>';
                }

                // Controle de Status e Parecer Técnico da Equipe Locar
                h += '<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;padding:10px 12px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;">' +
                     '<div style="min-width:165px;">' +
                     '<label style="display:block;font-size:0.72rem;font-weight:700;color:#475569;margin-bottom:3px;">Status da Análise</label>' +
                     '<select id="solSelectNovoStatus" class="cr-select" style="padding:6px 8px;font-size:0.80rem;">' +
                     ['PENDENTE', 'EM_ANALISE', 'APROVADA', 'CONCLUIDA', 'REJEITADA'].map(function (stOpt) {
                         var lbl = { PENDENTE: '⏳ Pendente', EM_ANALISE: '🔎 Em Análise', APROVADA: '✅ Aprovada', CONCLUIDA: '🏁 Concluída no GIS', REJEITADA: '❌ Rejeitada' }[stOpt];
                         return '<option value="' + stOpt + '"' + (s.status === stOpt ? ' selected' : '') + '>' + lbl + '</option>';
                     }).join('') +
                     '</select></div>' +
                     '<div style="flex:1;min-width:220px;">' +
                     '<label style="display:block;font-size:0.72rem;font-weight:700;color:#475569;margin-bottom:3px;">Parecer / Notas Internas Locar</label>' +
                     '<input type="text" id="solInputNotas" class="cr-input" style="padding:6px 10px;font-size:0.80rem;" placeholder="Ex.: Ajustado no QGIS por João e atualizado na rota..." value="' + esc(s.notas_analise || '') + '" />' +
                     '</div>' +
                     '<button type="button" class="cr-btn" id="btnSolSalvarStatus" style="padding:7px 14px;font-size:0.80rem;">💾 Salvar</button>' +
                     '<button type="button" class="cr-btn-sec" id="btnSolExcluir" style="padding:7px 10px;font-size:0.80rem;color:#DC2626;border-color:#FECACA;">🗑️ Excluir</button>' +
                     '</div>';

                // Tabela de itens solicitados
                h += '<div><div style="font-size:0.82rem;font-weight:800;color:#006331;margin-bottom:6px;">ITENS SOLICITADOS (' + (s.pedidos || []).length + ')</div>';
                h += '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.78rem;">' +
                     '<thead><tr style="background:#ECFDF5;color:#065F46;text-align:left;">' +
                     '<th style="padding:7px 8px;">Setor</th><th style="padding:7px 8px;">Alteração</th><th style="padding:7px 8px;">Ruas</th>' +
                     '<th style="padding:7px 8px;">Trechos / Ext.</th><th style="padding:7px 8px;">Detalhe / Motivo</th></tr></thead><tbody>';
                (s.pedidos || []).forEach(function (p) {
                    var ac = ACOES[p.tipo] || { nome: p.acao_nome || p.tipo, cor: '#038242', ic: '📌' };
                    var ruasTxt = Array.isArray(p.ruas_nomes) && p.ruas_nomes.length ? p.ruas_nomes.join(', ') : '—';
                    var detMot = [p.detalhe, p.obs ? '“' + p.obs + '”' : ''].filter(Boolean).join(' · ') || '—';
                    h += '<tr style="border-bottom:1px solid #E2E8F0;">' +
                         '<td style="padding:7px 8px;font-weight:700;">' + esc(p.setor || '—') + '</td>' +
                         '<td style="padding:7px 8px;"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + ac.cor + ';margin-right:5px;"></span><b>' + esc(ac.nome) + '</b></td>' +
                         '<td style="padding:7px 8px;">' + esc(ruasTxt) + '</td>' +
                         '<td style="padding:7px 8px;white-space:nowrap;">' + (p.trechos_qtd || 0) + ' · ' + fmtM(Number(p.extensao_m || 0)) + '</td>' +
                         '<td style="padding:7px 8px;color:#475569;">' + esc(detMot) + '</td>' +
                         '</tr>';
                });
                h += '</tbody></table></div></div>';

                // Etapa Final: Upload do .gpkg editado no QGIS -> Conversão Shapefile -> Publicação Inlog
                h += renderizarHtmlPainelGpkgInlog(s.contrato_id, (s.setores && s.setores[0]) || '', s.id);

                detBox.innerHTML = h;
                inicializarEventosPainelGpkgInlog(s.contrato_id, (s.setores && s.setores[0]) || '', s.id, s.setores || []);

                if ($('btnSolTogglePdf')) {
                    $('btnSolTogglePdf').onclick = function () {
                        var w = $('solPdfViewerWrap');
                        if (!w) return;
                        var vis = w.style.display !== 'none';
                        w.style.display = vis ? 'none' : 'block';
                        this.textContent = vis ? '👁️ Mostrar PDF na Tela' : '🙈 Ocultar PDF';
                    };
                }
                if ($('btnSolSalvarStatus')) {
                    $('btnSolSalvarStatus').onclick = function () {
                        var nv = $('solSelectNovoStatus').value;
                        var nt = $('solInputNotas').value;
                        fetch('/api/correcao-rotas/status', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ id: s.id, status: nv, notas_analise: nt })
                        }).then(function (r) { return r.json(); }).then(function (res) {
                            if (res && res.sucesso) {
                                crToast('Parecer e status salvos com sucesso!');
                                carregarListaSolicitacoesAdmin();
                            } else {
                                crToast('Erro ao salvar status.');
                            }
                        });
                    };
                }
                if ($('btnSolExcluir')) {
                    $('btnSolExcluir').onclick = function () {
                        if (!confirm('Deseja realmente excluir o protocolo ' + s.protocolo + '?')) return;
                        fetch('/api/correcao-rotas/excluir', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ id: s.id })
                        }).then(function (r) { return r.json(); }).then(function (res) {
                            if (res && res.sucesso) {
                                solSelecionadaId = null;
                                crToast('Solicitação excluída.');
                                carregarListaSolicitacoesAdmin();
                            }
                        });
                    };
                }
                if ($('btnSolVerNoMapa') && s.geojson) {
                    $('btnSolVerNoMapa').onclick = function () {
                        visualizarGeoJsonSolicitacaoNoMapa(s);
                    };
                }
            });
    }

    var _gpkgPollTimer = null;
    var _gpkgFileAtual = null;

    var CIDADES_INLOG_OPCOES = [
        ['cabo', 'Cabo de Santo Agostinho'],
        ['jaboatao', 'Jaboatão dos Guararapes'],
        ['olinda', 'Olinda'],
        ['paulista', 'Paulista'],
        ['caruaru', 'Caruaru'],
        ['cuiaba', 'Cuiabá'],
        ['garanhuns', 'Garanhuns'],
        ['pesqueira', 'Pesqueira'],
        ['vitoria', 'Vitória de Santo Antão'],
        ['canela', 'Canela'],
        ['jequie', 'Jequié'],
        ['novo hamburgo', 'Novo Hamburgo']
    ];

    function renderizarHtmlPainelGpkgInlog(contratoId, setorInicial, solId) {
        var cid = String(contratoId || 'garanhuns').toLowerCase().trim();
        if (cid === 'all' || !cid) cid = 'garanhuns';
        var cOpts = CIDADES_INLOG_OPCOES.map(function (c) {
            return '<option value="' + esc(c[0]) + '"' + (c[0] === cid ? ' selected' : '') + '>' + esc(c[1]) + '</option>';
        }).join('');

        return '<div class="sol-gpkg-card" id="solGpkgPublishCard">' +
            '<div class="sol-gpkg-header">' +
            '<div class="sol-gpkg-title">🚀 Etapa Final: Subir .gpkg Editado (Converter Shapefile + Publicar na Inlog)</div>' +
            '<span style="font-size:0.71rem;color:#047857;font-weight:700;">Automação QGIS → .shp (.zip) → Inlog EditorRotas</span>' +
            '</div>' +
            '<div class="sol-gpkg-drop">' +
            '<div style="flex:1;min-width:200px;">' +
            '<div style="font-size:0.78rem;font-weight:700;color:#0F172A;" id="solGpkgFileLabel">Nenhum arquivo .gpkg selecionado</div>' +
            '<div style="font-size:0.71rem;color:#64748B;">Selecione o arquivo .gpkg atualizado no QGIS (ex.: SCPN 19.gpkg)</div>' +
            '</div>' +
            '<button type="button" class="cr-btn-sec" id="btnSolEscolherGpkg" style="border-color:#16A34A;color:#065F46;font-weight:700;">📂 Selecionar .gpkg</button>' +
            '<input type="file" id="inputSolGpkgFile" accept=".gpkg" style="display:none;" />' +
            '</div>' +
            '<div class="sol-gpkg-grid">' +
            '<div class="sol-gpkg-field">' +
            '<label for="solGpkgCidadeSelect">Cidade / Perfil Inlog</label>' +
            '<select id="solGpkgCidadeSelect" class="cr-select" style="padding:6px 8px;font-size:0.79rem;">' + cOpts + '</select>' +
            '</div>' +
            '<div class="sol-gpkg-field">' +
            '<label for="solGpkgSetorSelect">Setor da Rota</label>' +
            '<select id="solGpkgSetorSelect" class="cr-select" style="padding:6px 8px;font-size:0.79rem;"><option value="">Carregando setores...</option></select>' +
            '</div>' +
            '<div class="sol-gpkg-field">' +
            '<label for="solGpkgSetorInlog">Descrição do Setor (campo SETOR / Inlog)</label>' +
            '<input type="text" id="solGpkgSetorInlog" class="cr-input" style="padding:6px 9px;font-size:0.79rem;" placeholder="Ex.: GARANHUNS - SCPN 19" />' +
            '</div>' +
            '<div class="sol-gpkg-field">' +
            '<label for="solGpkgMacroInlog">Macro Setor (campo MACRO)</label>' +
            '<input type="text" id="solGpkgMacroInlog" class="cr-input" style="padding:6px 9px;font-size:0.79rem;" placeholder="Ex.: GARANHUNS - DOMICILIAR" />' +
            '</div>' +
            '<div class="sol-gpkg-field">' +
            '<label for="solGpkgRotaNome">Nome da Rota (campo ROTA)</label>' +
            '<input type="text" id="solGpkgRotaNome" class="cr-input" style="padding:6px 9px;font-size:0.79rem;" value="ROTA SUB" />' +
            '</div>' +
            '<div class="sol-gpkg-field">' +
            '<label for="solGpkgApelido">Apelido da Rota (campo APELIDO)</label>' +
            '<input type="text" id="solGpkgApelido" class="cr-input" style="padding:6px 9px;font-size:0.79rem;" value="ROTA SUB" />' +
            '</div>' +
            '</div>' +
            '<div class="sol-gpkg-checks">' +
            '<label><input type="checkbox" id="solGpkgChkAtualizarSetorGis" checked /> Gerar Shapefile do Setor e atualizar no GIS Eyes (clientetvt4.exe — gabriellima)</label>' +
            '<label><input type="checkbox" id="solGpkgChkAtualizarLocal" checked /> Atualizar também o .gpkg oficial em "Rotas Planejadas" e reconstruir mapa do Sistema LC</label>' +
            '<label><input type="checkbox" id="solGpkgChkPublicarInlog" checked /> Logar na Inlog (carlos.dorth), selecionar cidade, inativar/excluir rota antiga, importar .shp e ativar nova rota</label>' +
            '</div>' +
            '<div class="sol-gpkg-actions">' +
            '<button type="button" class="cr-btn-sec" id="btnSolConverterShpZip" style="border-color:#038242;color:#006331;">📦 Baixar Shape Rota (.zip)</button>' +
            '<button type="button" class="cr-btn-sec" id="btnSolConverterSetorZip" style="border-color:#0284C7;color:#0369A1;">🗺️ Baixar Shape Setor (.zip)</button>' +
            '<button type="button" class="cr-btn" id="btnSolPublicarInlog" style="padding:8px 14px;font-size:0.80rem;">🚀 Converter, Atualizar Sistema e Publicar na Inlog</button>' +
            '<span id="solGpkgZipExtraWrap"></span>' +
            '</div>' +
            '<div id="solGpkgProgressWrap" class="sol-gpkg-progress-wrap" style="display:none;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;">' +
            '<strong id="solGpkgProgEtapa">Preparando...</strong>' +
            '<strong id="solGpkgProgPct" style="color:#34D399;">0%</strong>' +
            '</div>' +
            '<div class="sol-gpkg-bar-track"><div class="sol-gpkg-bar-fill" id="solGpkgProgBar"></div></div>' +
            '<div class="sol-gpkg-logs" id="solGpkgProgLogs"></div>' +
            '<div id="solGpkgProgLinks" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;"></div>' +
            '</div>' +
            '</div>';
    }

    function abrirPainelPublicarGpkgDireto() {
        solSelecionadaId = null;
        Array.prototype.forEach.call(document.querySelectorAll('#solListaContainer .sol-card-item'), function (el) {
            el.classList.remove('ativo');
        });
        var detBox = $('solDetalheContainer');
        if (!detBox) return;
        var selCidEl = document.getElementById('selectCidade');
        var cidAtual = (selCidEl && selCidEl.value && selCidEl.value !== 'all') ? selCidEl.value : 'garanhuns';
        var h = '<div style="border-bottom:1px solid #E2E8F0;padding-bottom:8px;">' +
            '<div style="font-size:1.0rem;font-weight:800;color:#0F172A;">Publicação Direta de Rota Editada (.gpkg)</div>' +
            '<div style="font-size:0.78rem;color:#64748B;">Converta o .gpkg editado no QGIS para Shapefile (.zip), atualize o Sistema LC e publique automaticamente no Editor de Rotas da Inlog.</div>' +
            '</div>' +
            renderizarHtmlPainelGpkgInlog(cidAtual, '', '');
        detBox.innerHTML = h;
        inicializarEventosPainelGpkgInlog(cidAtual, '', '', []);
    }

    function inicializarEventosPainelGpkgInlog(contratoId, setorInicial, solId, setoresDaSolic) {
        if (_gpkgPollTimer) { clearInterval(_gpkgPollTimer); _gpkgPollTimer = null; }
        _gpkgFileAtual = null;

        var cidSelect = $('solGpkgCidadeSelect');
        var setorSelect = $('solGpkgSetorSelect');
        var inpFile = $('inputSolGpkgFile');
        var btnFile = $('btnSolEscolherGpkg');

        function atualizarMetadadosSetorInlog(cid, codSetor) {
            if (!cid || !codSetor) return;
            fetch('/api/correcao-rotas/metadados-setor-inlog?cidade=' + encodeURIComponent(cid) + '&setor=' + encodeURIComponent(codSetor))
                .then(function (r) { return r.json(); })
                .then(function (j) {
                    if (!j || !j.sucesso || !j.meta) return;
                    if ($('solGpkgSetorInlog')) $('solGpkgSetorInlog').value = j.meta.descricao_inlog || '';
                    if ($('solGpkgMacroInlog')) $('solGpkgMacroInlog').value = j.meta.macro_setor || '';
                })
                .catch(function () {});
        }

        function carregarSetoresDaCidade(cid, setorPreferido) {
            if (!setorSelect) return;
            setorSelect.innerHTML = '<option value="">Carregando setores...</option>';
            fetch('/api/correcao-rotas/setores-contrato?cidade=' + encodeURIComponent(cid))
                .then(function (r) { return r.json(); })
                .then(function (j) {
                    var lista = (j && j.sucesso && Array.isArray(j.setores)) ? j.setores : [];
                    var codigosSet = {};
                    var opts = [];
                    (setoresDaSolic || []).forEach(function (cs) {
                        if (cs && !codigosSet[cs]) { codigosSet[cs] = 1; opts.push({ codigo: cs, agenda: 'Solicitação' }); }
                    });
                    lista.forEach(function (it) {
                        if (it && it.codigo && !codigosSet[it.codigo]) { codigosSet[it.codigo] = 1; opts.push(it); }
                    });
                    if (!opts.length) {
                        setorSelect.innerHTML = '<option value="">Nenhum setor encontrado</option>';
                        return;
                    }
                    setorSelect.innerHTML = opts.map(function (it) {
                        var extra = it.agenda ? ' · ' + it.agenda : '';
                        return '<option value="' + esc(it.codigo) + '">' + esc(it.codigo + extra) + '</option>';
                    }).join('');
                    var alvo = setorPreferido && codigosSet[setorPreferido] ? setorPreferido : opts[0].codigo;
                    setorSelect.value = alvo;
                    atualizarMetadadosSetorInlog(cid, alvo);
                })
                .catch(function () {
                    setorSelect.innerHTML = '<option value="">Erro ao listar setores</option>';
                });
        }

        function tentarDetectarSetorPeloNomeArquivo(nomeArq) {
            if (!nomeArq || !setorSelect) return;
            var normNome = String(nomeArq).toUpperCase().replace(/\.GPKG$/i, '').replace(/\(\d+\)/g, ' ').replace(/\s+/g, ' ').trim();
            var opcoes = Array.prototype.map.call(setorSelect.options, function (o) { return o.value; }).filter(Boolean);
            var melhor = null;
            opcoes.forEach(function (cod) {
                var cu = cod.toUpperCase();
                if (normNome.indexOf(cu) >= 0) {
                    if (!melhor || cu.length > melhor.length) melhor = cod;
                }
            });
            if (!melhor) {
                var m = normNome.match(/\b(SC[A-Z]{1,6})\s*0*(\d{1,4})\b/);
                if (m) {
                    var pref = m[1], num = parseInt(m[2], 10);
                    opcoes.forEach(function (cod) {
                        var mc = cod.toUpperCase().match(/\b(SC[A-Z]{1,6})\s*0*(\d{1,4})\b/);
                        if (mc && mc[1] === pref && parseInt(mc[2], 10) === num) melhor = cod;
                    });
                }
            }
            if (melhor) {
                setorSelect.value = melhor;
                atualizarMetadadosSetorInlog(cidSelect.value, melhor);
                crToast('Setor "' + melhor + '" detectado pelo nome do arquivo!');
            }
        }

        if (cidSelect) {
            cidSelect.onchange = function () {
                carregarSetoresDaCidade(cidSelect.value, '');
            };
        }
        if (setorSelect) {
            setorSelect.onchange = function () {
                atualizarMetadadosSetorInlog(cidSelect ? cidSelect.value : contratoId, setorSelect.value);
            };
        }
        if (btnFile && inpFile) {
            btnFile.onclick = function () { inpFile.click(); };
            inpFile.onchange = function () {
                var f = this.files && this.files[0];
                if (!f) return;
                _gpkgFileAtual = f;
                if ($('solGpkgFileLabel')) $('solGpkgFileLabel').textContent = '✅ ' + f.name + ' (' + Math.round(f.size / 1024) + ' KB)';
                tentarDetectarSetorPeloNomeArquivo(f.name);
            };
        }

        function coletarParamsGpkg() {
            if (!_gpkgFileAtual) {
                crToast('Selecione primeiro o arquivo .gpkg editado no QGIS.');
                return null;
            }
            var cid = (cidSelect && cidSelect.value) || contratoId;
            var setor = (setorSelect && setorSelect.value) || '';
            if (!cid || !setor) {
                crToast('Escolha a cidade e o setor de destino.');
                return null;
            }
            return {
                file: _gpkgFileAtual,
                contrato_id: cid,
                setor_codigo: setor,
                setor_descricao_inlog: ($('solGpkgSetorInlog') && $('solGpkgSetorInlog').value.trim()) || '',
                macro_setor: ($('solGpkgMacroInlog') && $('solGpkgMacroInlog').value.trim()) || '',
                rota_nome: ($('solGpkgRotaNome') && $('solGpkgRotaNome').value.trim()) || 'ROTA SUB',
                apelido_rota: ($('solGpkgApelido') && $('solGpkgApelido').value.trim()) || 'ROTA SUB',
                atualizar_setor_gis: $('solGpkgChkAtualizarSetorGis') ? $('solGpkgChkAtualizarSetorGis').checked : true,
                atualizar_local: $('solGpkgChkAtualizarLocal') ? $('solGpkgChkAtualizarLocal').checked : true,
                publicar_inlog: $('solGpkgChkPublicarInlog') ? $('solGpkgChkPublicarInlog').checked : true,
                sol_id: solId || ''
            };
        }

        function dispararConversaoZip(btn, labelPadrao, tipoConv) {
            var params = coletarParamsGpkg();
            if (!params) return;
            btn.disabled = true;
            btn.textContent = '⏳ Convertendo...';
            blobParaBase64(params.file).then(function (b64) {
                return fetch('/api/correcao-rotas/converter-gpkg-shp', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        tipo: tipoConv,
                        gpkg_base64: b64,
                        contrato_id: params.contrato_id,
                        setor_codigo: params.setor_codigo,
                        setor_descricao_inlog: params.setor_descricao_inlog,
                        macro_setor: params.macro_setor,
                        rota_nome: params.rota_nome,
                        apelido_rota: params.apelido_rota
                    })
                }).then(function (r) { return r.json(); });
            }).then(function (res) {
                btn.disabled = false;
                btn.textContent = labelPadrao;
                if (!res || !res.sucesso) {
                    crToast('Erro ao converter: ' + ((res && res.erro) || 'falha desconhecida'));
                    return;
                }
                var bin = atob(res.zip_base64);
                var bytes = new Uint8Array(bin.length);
                for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                baixarBlob(new Blob([bytes], { type: 'application/zip' }), res.zip_filename || (tipoConv === 'setor' ? 'setor.zip' : 'rotas.zip'));
                var rsm = res.resumo || {};
                if (tipoConv === 'setor') {
                    crToast('Shapefile do Setor "' + res.zip_filename + '" gerado (' + (rsm.clusters || 1) + ' cluster(s), Op: ' + (rsm.operacao_giseyes || 'DOMICILIAR F1') + ')!');
                } else {
                    crToast('Shapefile "' + res.zip_filename + '" gerado (' + (rsm.trechos_shapefile || 0) + ' trechos, ' + (rsm.km_coleta || 0) + ' km coleta)!');
                }
            }).catch(function (e) {
                btn.disabled = false;
                btn.textContent = labelPadrao;
                crToast('Falha na conversão: ' + e.message);
            });
        }

        if ($('btnSolConverterShpZip')) {
            $('btnSolConverterShpZip').onclick = function () {
                dispararConversaoZip(this, '📦 Baixar Shape Rota (.zip)', 'rota');
            };
        }
        if ($('btnSolConverterSetorZip')) {
            $('btnSolConverterSetorZip').onclick = function () {
                dispararConversaoZip(this, '🗺️ Baixar Shape Setor (.zip)', 'setor');
            };
        }

        if ($('btnSolPublicarInlog')) {
            $('btnSolPublicarInlog').onclick = function () {
                var params = coletarParamsGpkg();
                if (!params) return;
                if (!params.atualizar_setor_gis && !params.atualizar_local && !params.publicar_inlog) {
                    crToast('Marque ao menos uma ação (GIS Eyes, Sistema LC ou Inlog).');
                    return;
                }
                var btn = this;
                btn.disabled = true;
                btn.textContent = '⏳ Executando automação...';
                var pWrap = $('solGpkgProgressWrap');
                if (pWrap) pWrap.style.display = 'block';
                if ($('solGpkgProgEtapa')) $('solGpkgProgEtapa').textContent = 'Enviando arquivo .gpkg para o servidor...';
                if ($('solGpkgProgPct')) $('solGpkgProgPct').textContent = '5%';
                if ($('solGpkgProgBar')) $('solGpkgProgBar').style.width = '5%';
                if ($('solGpkgProgLogs')) $('solGpkgProgLogs').textContent = '';
                if ($('solGpkgProgLinks')) $('solGpkgProgLinks').innerHTML = '';

                var logsPreAgente = [];
                function atualizarLogsUI(logsSrv) {
                    if ($('solGpkgProgLogs')) {
                        var todos = logsPreAgente.concat(logsSrv || []);
                        $('solGpkgProgLogs').textContent = todos.join('\n');
                        $('solGpkgProgLogs').scrollTop = $('solGpkgProgLogs').scrollHeight;
                    }
                }

                function checarAgenteGisLocal() {
                    var ehLocal = (window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost');
                    if (ehLocal || !params.atualizar_setor_gis) return Promise.resolve(false);
                    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
                    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 1300) : null;
                    return fetch('http://127.0.0.1:18765/health', ctrl ? { signal: ctrl.signal } : {})
                        .then(function (r) { if (timer) clearTimeout(timer); return r.json(); })
                        .then(function (j) { return !!(j && j.ok); })
                        .catch(function () { if (timer) clearTimeout(timer); return false; });
                }

                blobParaBase64(params.file).then(function (b64) {
                    return checarAgenteGisLocal().then(function (agenteAtivo) {
                        if (!agenteAtivo) {
                            return { b64: b64, atualizarNoServidor: params.atualizar_setor_gis };
                        }
                        if ($('solGpkgProgEtapa')) $('solGpkgProgEtapa').textContent = 'Agente local Windows detectado! Gerando Shape do Setor para GIS Eyes...';
                        if ($('solGpkgProgPct')) $('solGpkgProgPct').textContent = '15%';
                        if ($('solGpkgProgBar')) $('solGpkgProgBar').style.width = '15%';
                        logsPreAgente.push('[Agente Local] 🖥️ Agente Windows (127.0.0.1:18765) detectado. Gerando Shape do Setor...');
                        atualizarLogsUI([]);

                        return fetch('/api/correcao-rotas/converter-gpkg-shp', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                tipo: 'setor',
                                gpkg_base64: b64,
                                contrato_id: params.contrato_id,
                                setor_codigo: params.setor_codigo,
                                setor_descricao_inlog: params.setor_descricao_inlog,
                                macro_setor: params.macro_setor,
                                sincronizar_inlog: true
                            })
                        }).then(function (r) { return r.json(); }).then(function (convSetor) {
                            if (!convSetor || !convSetor.sucesso || !convSetor.zip_base64) {
                                throw new Error((convSetor && convSetor.erro) || 'Falha ao gerar Shapefile do Setor.');
                            }
                            if (Array.isArray(convSetor.logs_sync)) {
                                convSetor.logs_sync.forEach(function (l) { logsPreAgente.push('[SetoresBack] ' + l); });
                            }
                            if ($('solGpkgProgEtapa')) $('solGpkgProgEtapa').textContent = 'Executando clientetvt4.exe (GIS Eyes) no seu computador Windows...';
                            if ($('solGpkgProgPct')) $('solGpkgProgPct').textContent = '30%';
                            if ($('solGpkgProgBar')) $('solGpkgProgBar').style.width = '30%';
                            logsPreAgente.push('[Agente Local] 🚀 Enviando ' + (convSetor.zip_filename || 'setor.zip') + ' para clientetvt4.exe...');
                            atualizarLogsUI([]);

                            return fetch('http://127.0.0.1:18765/atualizar-setor', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    zip_setor_base64: convSetor.zip_base64,
                                    zip_filename: convSetor.zip_filename || '',
                                    contrato_id: params.contrato_id
                                })
                            }).then(function (r) { return r.json(); }).then(function (agRes) {
                                if (agRes && Array.isArray(agRes.logs)) {
                                    agRes.logs.forEach(function (l) { logsPreAgente.push('[Agente Local] ' + l); });
                                }
                                if (!agRes || !agRes.sucesso) {
                                    throw new Error('Agente Local GIS Eyes: ' + ((agRes && agRes.erro) || 'falha desconhecida'));
                                }
                                atualizarLogsUI([]);
                                return fetch('/api/rotas/inlog/deduplicar_setor', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({
                                        contrato_id: params.contrato_id,
                                        setor_codigo: params.setor_codigo,
                                        resumo_setor: convSetor.resumo || {}
                                    })
                                }).then(function (r) { return r.json(); }).then(function (ddRes) {
                                    if (ddRes && Array.isArray(ddRes.logs)) {
                                        ddRes.logs.forEach(function (l) { logsPreAgente.push('[Pós-GIS] ' + l); });
                                    }
                                    atualizarLogsUI([]);
                                    return {
                                        b64: b64,
                                        atualizarNoServidor: false,
                                        setorDescricaoAtual: (convSetor.resumo && convSetor.resumo.setor) || params.setor_descricao_inlog
                                    };
                                }).catch(function () {
                                    return {
                                        b64: b64,
                                        atualizarNoServidor: false,
                                        setorDescricaoAtual: (convSetor.resumo && convSetor.resumo.setor) || params.setor_descricao_inlog
                                    };
                                });
                            });
                        });
                    });
                }).then(function (ctx) {
                    return fetch('/api/correcao-rotas/publicar-gpkg-inlog', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            gpkg_base64: ctx.b64,
                            contrato_id: params.contrato_id,
                            setor_codigo: params.setor_codigo,
                            setor_descricao_inlog: ctx.setorDescricaoAtual || params.setor_descricao_inlog,
                            macro_setor: params.macro_setor,
                            rota_nome: params.rota_nome,
                            apelido_rota: params.apelido_rota,
                            atualizar_setor_gis: ctx.atualizarNoServidor,
                            atualizar_local: params.atualizar_local,
                            publicar_inlog: params.publicar_inlog,
                            sol_id: params.sol_id
                        })
                    }).then(function (r) { return r.json(); });
                }).then(function (startRes) {
                    if (!startRes || !startRes.sucesso || !startRes.job_id) {
                        btn.disabled = false;
                        btn.textContent = '🚀 Converter, Atualizar Sistema e Publicar na Inlog';
                        crToast('Erro ao iniciar publicação: ' + ((startRes && startRes.erro) || 'falha'));
                        return;
                    }
                    var jobId = startRes.job_id;
                    if (_gpkgPollTimer) clearInterval(_gpkgPollTimer);
                    _gpkgPollTimer = setInterval(function () {
                        fetch('/api/correcao-rotas/publicar-status?job_id=' + encodeURIComponent(jobId))
                            .then(function (r) { return r.json(); })
                            .then(function (st) {
                                if (!st || !st.sucesso) return;
                                var pct = Number(st.progresso || 0);
                                if ($('solGpkgProgEtapa')) $('solGpkgProgEtapa').textContent = st.etapa_atual || '';
                                if ($('solGpkgProgPct')) $('solGpkgProgPct').textContent = pct + '%';
                                if ($('solGpkgProgBar')) $('solGpkgProgBar').style.width = pct + '%';
                                atualizarLogsUI(st.logs || []);

                                if (st.status === 'CONCLUIDO' || st.status === 'ERRO') {
                                    clearInterval(_gpkgPollTimer);
                                    _gpkgPollTimer = null;
                                    btn.disabled = false;
                                    btn.textContent = '🚀 Converter, Atualizar Sistema e Publicar na Inlog';

                                    var linksHtml = '';
                                    if (st.tem_zip) {
                                        linksHtml += '<a class="cr-btn-sec" href="/api/correcao-rotas/publicar-download-zip?job_id=' + encodeURIComponent(jobId) + '" style="text-decoration:none;background:#FFFFFF;">⬇ Baixar ' + esc(st.zip_filename || 'rotas.zip') + '</a>';
                                    }
                                    if (st.tem_zip_setor) {
                                        linksHtml += '<a class="cr-btn-sec" href="/api/correcao-rotas/publicar-download-zip?job_id=' + encodeURIComponent(jobId) + '&tipo=setor" style="text-decoration:none;background:#FFFFFF;">🗺️ Baixar ' + esc(st.zip_setor_filename || 'setor.zip') + '</a>';
                                    }
                                    if (st.status === 'CONCLUIDO' && st.resultado && st.resultado.inlog && st.resultado.inlog.url_editor) {
                                        linksHtml += '<a class="cr-btn" href="' + esc(st.resultado.inlog.url_editor) + '" target="_blank" style="text-decoration:none;padding:7px 12px;font-size:0.78rem;">↗ Abrir Rota na Inlog</a>';
                                    }
                                    if ($('solGpkgProgLinks')) $('solGpkgProgLinks').innerHTML = linksHtml;

                                    if (st.status === 'CONCLUIDO') {
                                        crToast('Rota convertida e publicada com sucesso!');
                                        if ($('solSelectNovoStatus')) $('solSelectNovoStatus').value = 'CONCLUIDA';
                                        atualizarBadgePendentesAdmin();
                                        if (params.sol_id) {
                                            var cardItem = document.querySelector('#solListaContainer .sol-card-item[data-id="' + params.sol_id + '"] .sol-status-pill');
                                            if (cardItem) {
                                                cardItem.className = 'sol-status-pill sol-status-CONCLUIDA';
                                                cardItem.textContent = 'Concluída';
                                            }
                                        }
                                        if (params.atualizar_local && window.__SISTEMA_LC__) {
                                            var selC = document.getElementById('selectCidade');
                                            if (selC && selC.value === params.contrato_id) {
                                                var btnRef = document.getElementById('btnAtualizar');
                                                if (btnRef) btnRef.click();
                                            }
                                        }
                                    } else {
                                        crToast('Erro na automação: ' + (st.erro || 'verifique o log'));
                                    }
                                }
                            })
                            .catch(function () {});
                    }, 1100);
                }).catch(function (e) {
                    btn.disabled = false;
                    btn.textContent = '🚀 Converter, Atualizar Sistema e Publicar na Inlog';
                    crToast('Falha ao iniciar publicação: ' + e.message);
                });
            };
        }

        carregarSetoresDaCidade(cidSelect ? cidSelect.value : contratoId, setorInicial);
    }

    function visualizarGeoJsonSolicitacaoNoMapa(sol) {
        garantirCamadasNoMapa();
        var m = getMap();
        if (!m || !sol.geojson) return;
        $('modalAdminSolicitacoes').style.display = 'none';
        if (correcaoAtiva) alternarAbaCorrecaoRotas(false);

        camadaAdminPreview.clearLayers();
        camadaAdminPreview.addTo(m);
        var bounds = null;
        (sol.geojson.features || []).forEach(function (f) {
            if (!f.geometry || !f.geometry.coordinates) return;
            var ll = f.geometry.coordinates.map(function (c) { return [c[1], c[0]]; });
            ll.forEach(function (pt) { if (!bounds) bounds = L.latLngBounds(pt, pt); else bounds.extend(pt); });
            var pr = f.properties || {};
            var ac = ACOES[pr.acao] || ACOES.manual;
            L.polyline(ll, { color: '#111111', weight: 10, opacity: 0.85 }).addTo(camadaAdminPreview);
            var pl = L.polyline(ll, { color: ac.cor, weight: 6, opacity: 1.0 }).addTo(camadaAdminPreview);
            pl.bindPopup(
                '<b>' + esc(sol.protocolo) + ' · ' + esc(ac.nome) + '</b><br>' +
                'Rua: <b>' + esc(pr.rua || 'Sem nome') + '</b><br>' +
                'Setor: <b>' + esc(pr.setor || '—') + '</b>' + (pr.setor_destino ? ' → <b>' + esc(pr.setor_destino) + '</b>' : '') +
                (pr.observacao ? '<br>Motivo: ' + esc(pr.observacao) : '')
            );
        });
        if (bounds) m.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
        crToast('Exibindo alterações do protocolo ' + sol.protocolo + ' destacadas no mapa.');
    }

    function importarPdfAvulsoAdmin(file) {
        if (!file) return;
        blobParaBase64(file).then(function (b64) {
            return fetch('/api/correcao-rotas/importar-pdf', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pdf_base64: b64, filename: file.name })
            }).then(function (r) { return r.json(); });
        }).then(function (res) {
            if (res && res.sucesso) {
                solSelecionadaId = res.id;
                crToast('PDF importado com sucesso! Protocolo: ' + res.protocolo);
                carregarListaSolicitacoesAdmin();
            } else {
                crToast('Erro ao importar PDF: ' + ((res && res.erro) || 'arquivo inválido'));
            }
        }).catch(function (e) {
            crToast('Falha ao importar PDF: ' + e.message);
        });
    }

    function extrairGeoJsonLocalDePdf(file) {
        if (!file) return;
        file.arrayBuffer().then(function (ab) {
            var u8 = new Uint8Array(ab), txt = new TextDecoder('latin1').decode(u8);
            var k = txt.indexOf('/Type /EmbeddedFile');
            var mL = k >= 0 ? /\/Length\s+(\d+)/.exec(txt.slice(k, k + 350)) : null;
            if (!mL) {
                crToast('Este PDF não possui arquivo técnico .geojson embutido.');
                return;
            }
            var sIdx = txt.indexOf('stream\n', k);
            var ini = sIdx >= 0 ? sIdx + 7 : txt.indexOf('stream\r\n', k) + 8;
            var len = Number(mL[1]);
            baixarBlob(new Blob([u8.slice(ini, ini + len)], { type: 'application/geo+json' }), file.name.replace(/\.pdf$/i, '') + '.geojson');
            crToast('Arquivo técnico .geojson extraído com sucesso para o QGIS!');
        });
    }

    // ======================================================================
    // INICIALIZAÇÃO DE EVENTOS DE UI
    // ======================================================================
    function bindEventosUI() {
        var btnAbrir = $('btnAbrirCorrecaoRotas');
        if (btnAbrir) btnAbrir.addEventListener('click', function () { alternarAbaCorrecaoRotas(); });

        var btnFecharAba = $('crBtnFecharAba');
        if (btnFecharAba) btnFecharAba.addEventListener('click', function () { alternarAbaCorrecaoRotas(false); });

        Array.prototype.forEach.call(document.querySelectorAll('#crPainelLateral .cr-modo'), function (b) {
            b.addEventListener('click', function () { trocarModo(b.dataset.modo); });
        });

        var buscaRua = $('crBuscaRua');
        if (buscaRua) {
            buscaRua.addEventListener('change', function () {
                var r = this.value, m = getMap();
                if (!r || !D || !m) return;
                var bb = null;
                D.lista.forEach(function (s) {
                    if (s.rua === r && setorVisivel(s.code)) {
                        s.ll.forEach(function (p) { if (!bb) bb = L.latLngBounds(p, p); else bb.extend(p); });
                    }
                });
                if (bb) m.fitBounds(bb, { padding: [60, 60], maxZoom: 18 });
                else crToast('Rua não encontrada nos setores visíveis.');
            });
        }

        if ($('crNomeSolic')) $('crNomeSolic').addEventListener('input', salvarRascunho);
        if ($('crObsGeral')) $('crObsGeral').addEventListener('input', salvarRascunho);

        if ($('crBtApagarTudo')) {
            $('crBtApagarTudo').addEventListener('click', function () {
                if (!confirm('Apagar todas as alterações desta solicitação?')) return;
                pedidos = [];
                salvarRascunho();
                desenharPedidosMapa();
                renderPedidos();
                renderListaSetores();
                desenharLegenda();
            });
        }

        if ($('crBtPdf')) $('crBtPdf').addEventListener('click', gerarEnviarEBaixarPDF);

        if ($('crModalFundo')) {
            $('crModalFundo').addEventListener('mousedown', function (e) {
                if (e.target === $('crModalFundo')) fecharCrModal();
            });
        }

        document.addEventListener('keydown', function (e) {
            if (!correcaoAtiva) return;
            if (e.key === 'Escape') {
                if ($('crModalFundo') && $('crModalFundo').style.display === 'flex') fecharCrModal();
                else { selecao = {}; selMalha = {}; desenharSelecao(); renderSelecao(); }
            }
        });

        // Eventos do Painel Administrativo no Menu Principal
        if ($('btnAdminSolicitacoes')) {
            $('btnAdminSolicitacoes').addEventListener('click', abrirCentralSolicitacoesRotas);
        }
        if ($('btnFecharAdminSolicitacoes')) {
            $('btnFecharAdminSolicitacoes').addEventListener('click', function () {
                $('modalAdminSolicitacoes').style.display = 'none';
            });
        }
        if ($('solFiltroCidade')) $('solFiltroCidade').addEventListener('change', carregarListaSolicitacoesAdmin);
        if ($('solFiltroStatus')) $('solFiltroStatus').addEventListener('change', carregarListaSolicitacoesAdmin);
        if ($('btnSolAtualizarLista')) $('btnSolAtualizarLista').addEventListener('click', carregarListaSolicitacoesAdmin);

        if ($('btnSolImportarPdf') && $('inputSolImportarPdf')) {
            $('btnSolImportarPdf').addEventListener('click', function () { $('inputSolImportarPdf').click(); });
            $('inputSolImportarPdf').addEventListener('change', function () {
                var f = this.files && this.files[0];
                this.value = '';
                if (f) importarPdfAvulsoAdmin(f);
            });
        }

        if ($('btnSolExtrairGeoAvulso') && $('inputSolExtrairGeoAvulso')) {
            $('btnSolExtrairGeoAvulso').addEventListener('click', function () { $('inputSolExtrairGeoAvulso').click(); });
            $('inputSolExtrairGeoAvulso').addEventListener('change', function () {
                var f = this.files && this.files[0];
                this.value = '';
                if (f) extrairGeoJsonLocalDePdf(f);
            });
        }

        if ($('btnSolPublicarGpkgAvulso')) {
            $('btnSolPublicarGpkgAvulso').addEventListener('click', abrirPainelPublicarGpkgDireto);
        }

        setTimeout(atualizarBadgePendentesAdmin, 1200);
    }

    // Hook chamado quando o mapa principal troca de cidade ou data com a aba aberta
    window.onAuditoriaAtualizadaCorrecao = function () {
        if (!correcaoAtiva) return;
        var lc = window.__SISTEMA_LC__;
        var audit = lc && lc.getAudit();
        if (!audit || !audit.resumo || String(audit.resumo.contrato_id || '').toLowerCase() === 'all') {
            alternarAbaCorrecaoRotas(false);
            return;
        }
        lc.suspenderMapaPadrao(true);
        if (construirDadosDoMapaAtual()) {
            iniciarPainelCorrecao();
        }
    };

    window.correcaoRotasLC = {
        abrir: function () { alternarAbaCorrecaoRotas(true); },
        fechar: function () { alternarAbaCorrecaoRotas(false); },
        abrirPainelAdmin: abrirCentralSolicitacoesRotas
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindEventosUI);
    } else {
        bindEventosUI();
    }
})();
