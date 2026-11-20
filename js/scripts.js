/* ================================================================
   js/scripts.js — BrewSense AI v3 · Conectado 100% a Flask + MariaDB
   ================================================================ */

document.addEventListener("DOMContentLoaded", function () {
    "use strict";

    const STATE = {
        user: null,
        rules: [],
        combos: [],
        products: [],
        entrenamientos: [],
        distribucionVentas: [],
        baseTicket: 85.0,
        lastTrainedAt: "—",
        lastTrainMs: null,
        timeSlot: "all",
        network: null,
    };

    let liftBarChart = null;
    let doughnutChart = null;
    let scatterChart = null;
    let comboModal = null;
    let habladorModal = null;
    let userModal = null;

    const $ = id => document.getElementById(id);
    const $$ = sel => document.querySelectorAll(sel);
    const setText = (id, val) => { const el = $(id); if (el) el.textContent = val; };
    const setHTML = (id, val) => { const el = $(id); if (el) el.innerHTML = val; };

    const TURNO_LABELS = { all: "Todo el día", morning: "Mañana", afternoon: "Tarde", night: "Noche" };

    function showToast(msg, type = "info") {
        const toastEl = $("brew-toast");
        const toastMsg = $("brew-toast-msg");
        if (!toastEl || !toastMsg) return;
        const colors = { success: "#198754", error: "#dc3545", info: "#BC804D", warning: "#fd7e14" };
        toastEl.style.backgroundColor = colors[type] || colors.info;
        toastEl.style.color = "#fff";
        toastMsg.textContent = msg;
        new bootstrap.Toast(toastEl, { delay: 3600 }).show();
    }

    function btnLoading(btnId, loading, originalHTML) {
        const btn = $(btnId);
        if (!btn) return;
        if (loading) {
            btn.disabled = true;
            btn.dataset.originalHtml = btn.innerHTML;
            btn.innerHTML = `<span class="spinner-btn me-2"></span>Procesando…`;
        } else {
            btn.disabled = false;
            btn.innerHTML = originalHTML || btn.dataset.originalHtml || "Listo";
        }
    }

    async function apiJson(url, options = {}) {
        const res = await fetch(url, options);
        if (res.status === 401) {
            window.location.href = "/login";
            throw new Error("Sesión expirada");
        }
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `Error HTTP ${res.status}`);
        return data;
    }

    // ── 1. SESIÓN Y CERRAR SESIÓN ──
    async function loadSessionUser() {
        try {
            const { user } = await apiJson("/api/auth/me");
            STATE.user = user;
            setText("sidebar-user-name", user.nombre);
            setText("sidebar-user-role", user.rol === "admin" ? "Administrador" : "Empleado");
            const initials = user.nombre.split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();
            setText("sidebar-user-avatar", initials || "CA");
        } catch {
            window.location.href = "/login";
        }
    }

    $("btn-logout") && $("btn-logout").addEventListener("click", async () => {
        await fetch("/api/auth/logout", { method: "POST" });
        window.location.href = "/login";
    });

    // ── 2. NAVEGACIÓN SPA + SINERGIA DE CABECERA + MENÚ MÓVIL ──
    const views = ["view-dashboard", "view-ingesta", "view-reglas", "view-combos", "view-config"];

    function showView(viewId) {
        views.forEach(v => { const el = $(v); if (el) el.classList.toggle("d-none", v !== viewId); });
        const activeLink = document.querySelector(`.nav-link[data-view="${viewId}"]`);
        if (activeLink) setText("topbar-title", activeLink.dataset.title || "");

        const dashFilters = $("dashboard-filters");
        const btnUpload = $("btn-top-upload");
        if (dashFilters) {
            dashFilters.classList.toggle("d-none", viewId !== "view-dashboard");
            dashFilters.classList.toggle("d-flex", viewId === "view-dashboard");
        }
        if (btnUpload) {
            btnUpload.classList.toggle("d-none", viewId !== "view-ingesta");
            btnUpload.classList.toggle("d-flex", viewId === "view-ingesta");
        }

        if (viewId === "view-reglas" && STATE.rules.length > 0) {
            setTimeout(() => buildNetworkGraph(STATE.rules), 120);
        }
        if (viewId === "view-config") {
            checkApiStatus();
            loadUsers();
        }
    }

    $$(".nav-link[data-view]").forEach(link => {
        link.addEventListener("click", e => {
            e.preventDefault();
            $$(".nav-link[data-view]").forEach(l => l.classList.remove("active"));
            link.classList.add("active");
            showView(link.dataset.view);
            if (window.innerWidth <= 768 && $("sidebar")) $("sidebar").classList.remove("show-mobile");
        });
    });

    if ($("btn-mobile-menu") && $("sidebar")) {
        $("btn-mobile-menu").addEventListener("click", () => $("sidebar").classList.toggle("show-mobile"));
    }

    // ── 3. CARGA DE DATOS DESDE MARIADB (GET /api/rules) ──
    async function fetchDashboardData(showNotify = false) {
        const sucursal = encodeURIComponent($("location-select")?.value || "all");
        const periodo = encodeURIComponent($("period-select")?.value || "365d");
        const turno = encodeURIComponent(STATE.timeSlot);
        const includeBlocked = $("chk-show-blocked")?.checked ? "1" : "0";

        const data = await apiJson(`/api/rules?sucursal=${sucursal}&periodo=${periodo}&turno=${turno}&all=${includeBlocked}`);
        applyServerPayload(data);
        if (showNotify) showToast("Datos sincronizados desde MariaDB", "success");
    }

    function applyServerPayload(data) {
        if (data.summary) applyEtlSummary(data.summary);
        if (data.kpis) applyKPIs(data.kpis);
        STATE.rules = data.rules || [];
        if (data.combos) STATE.combos = data.combos;
        if (data.entrenamientos) STATE.entrenamientos = data.entrenamientos;
        if (data.distribucion_ventas) STATE.distribucionVentas = data.distribucion_ventas;
        STATE.lastTrainedAt = data.trained_at || STATE.lastTrainedAt;
        if (data.tiempo_ms != null) STATE.lastTrainMs = data.tiempo_ms;
        renderAll();
    }
    
    // ── 4. SUBIDA DE CSV A MARIADB (POST /api/upload) ──
    const csvFileInput = $("csvFileInput");
    const triggerUpload = () => csvFileInput && csvFileInput.click();
    $("btn-top-upload") && $("btn-top-upload").addEventListener("click", triggerUpload);
    $("btn-process-etl") && $("btn-process-etl").addEventListener("click", triggerUpload);

    const dropzone = $("dropzone-area");
    if (dropzone) {
        dropzone.addEventListener("dragover", e => { e.preventDefault(); dropzone.classList.add("drag-over"); });
        dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag-over"));
        dropzone.addEventListener("drop", e => {
            e.preventDefault();
            dropzone.classList.remove("drag-over");
            if (e.dataTransfer.files[0]) uploadCSV(e.dataTransfer.files[0]);
        });
    }
    csvFileInput && csvFileInput.addEventListener("change", function () {
        if (this.files[0]) uploadCSV(this.files[0]);
    });

    async function uploadCSV(file) {
        if (!file.name.toLowerCase().endsWith(".csv")) return showToast("Solo se aceptan archivos .csv", "error");

        btnLoading("btn-process-etl", true);
        if ($("etl-progress-bar")) $("etl-progress-bar").style.width = "40%";
        setText("etl-step-text", "Importando CSV a MariaDB y reentrenando IA...");
        setText("etl-percent-text", "40%");

        const fd = new FormData();
        fd.append("file", file);

        try {
            const data = await apiJson("/api/upload", { method: "POST", body: fd });
            applyServerPayload(data);
            await loadProductsCatalog();
            showToast(`CSV importado a MariaDB (${(data.summary?.canastas_unicas || 0).toLocaleString("es-MX")} canastas)`, "success");
        } catch (err) {
            if ($("etl-progress-bar")) $("etl-progress-bar").style.width = "0%";
            setText("etl-step-text", `Error: ${err.message}`);
            showToast(err.message, "error");
        } finally {
            btnLoading("btn-process-etl", false, '<i class="fas fa-database me-2"></i>Importar CSV a MariaDB');
            if (csvFileInput) csvFileInput.value = "";
        }
    }

    function applyEtlSummary(s) {
        if (!s) return;
        if ($("etl-progress-bar")) $("etl-progress-bar").style.width = "100%";
        setText("etl-step-text", `Fuente activa: ${s.archivo || "MariaDB"}`);
        setText("etl-percent-text", "100%");
        setText("etl-raw-rows", Number(s.registros_brutos || 0).toLocaleString("es-MX"));
        setText("etl-nulls", `${Number(s.nulos_depurados || 0).toLocaleString("es-MX")} depurados`);
        setText("etl-baskets", Number(s.canastas_unicas || 0).toLocaleString("es-MX"));

        const tbody = $("etl-preview-tbody");
        if (tbody && Array.isArray(s.preview) && s.preview.length) {
            tbody.innerHTML = s.preview.map(r => `
                <tr>
                    <td><code class="small">${String(r.id_ticket).slice(0, 16)}</code></td>
                    <td class="text-muted small">${r.fecha}<br><span class="text-coffee">${r.sucursal}</span></td>
                    <td>${(r.articulos || []).map(p => `<span class="basket-tag">${p}</span>`).join("")}</td>
                    <td class="text-end fw-bold small">$${Number(r.total || 0).toFixed(2)}</td>
                </tr>
            `).join("");
        }
    }

    // ── 5. SLIDERS, FILTROS Y REENTRENAMIENTO FP-GROWTH ──
    function bindSlider(sliderId, labelId, suffix, decimals = 0) {
        const s = $(sliderId), l = $(labelId);
        if (!s || !l) return;
        const upd = () => { l.textContent = parseFloat(s.value).toFixed(decimals) + suffix; };
        upd();
        s.addEventListener("input", upd);
    }
    bindSlider("slider-dash-supp", "lbl-dash-supp", "%", 1);
    bindSlider("slider-dash-conf", "lbl-dash-conf", "%", 0);
    bindSlider("slider-dash-lift", "lbl-dash-lift", "", 1);
    bindSlider("slider-sim-conv", "lbl-sim-conv", "%", 0);

    $("slider-sim-conv") && $("slider-sim-conv").addEventListener("input", updateSimulador);

    $$(".time-seg-btn").forEach(btn => {
        btn.addEventListener("click", function () {
            $$(".time-seg-btn").forEach(b => b.classList.remove("active"));
            this.classList.add("active");
            STATE.timeSlot = this.dataset.slot;
            fetchDashboardData(false).catch(err => showToast(err.message, "error"));
        });
    });

    ["location-select", "period-select"].forEach(id => {
        $(id) && $(id).addEventListener("change", () => fetchDashboardData(false).catch(err => showToast(err.message, "error")));
    });

    $("btn-sync-live") && $("btn-sync-live").addEventListener("click", () => {
        fetchDashboardData(true).catch(err => showToast(err.message, "error"));
    });

    $("chk-show-blocked") && $("chk-show-blocked").addEventListener("change", () => {
        fetchDashboardData(false).catch(err => showToast(err.message, "error"));
    });

    $("btn-apply-params") && $("btn-apply-params").addEventListener("click", runModel);

    async function runModel() {
        const payload = {
            min_support: parseFloat($("slider-dash-supp")?.value || 1) / 100,
            min_confidence: parseFloat($("slider-dash-conf")?.value || 55) / 100,
            min_lift: parseFloat($("slider-dash-lift")?.value || 1.2),
            sucursal: $("location-select")?.value || "all",
            periodo: $("period-select")?.value || "365d",
            turno: STATE.timeSlot,
        };

        btnLoading("btn-apply-params", true);
        setText("chart-filter-badge", `Umbral ≥ ${payload.min_lift.toFixed(1)}`);

        try {
            const data = await apiJson("/api/predict", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            applyServerPayload(data);
            showToast(`FP-Growth ejecutado en ${data.tiempo_ms} ms (${data.rules_count} reglas)`, "success");
        } catch (err) {
            showToast(err.message, "error");
        } finally {
            btnLoading("btn-apply-params", false, '<i class="fas fa-sliders me-2"></i>Reentrenar Modelo en MariaDB');
        }
    }

    // ── 6. RENDERIZADO DE KPIS Y GRÁFICAS REALES ──
    function applyKPIs(kpis) {
        if (kpis.tickets_total != null) {
            setText("kpi-tickets", Number(kpis.tickets_total).toLocaleString("es-MX"));
            setHTML("kpi-tickets-sub", `<span class="text-success"><i class="fas fa-cash-register small"></i> ${kpis.tickets_pos || 0} ventas nuevas en POS</span>`);
        }
        if (kpis.ticket_promedio != null) {
            STATE.baseTicket = Number(kpis.ticket_promedio);
            setHTML("kpi-ticket-avg", `$${STATE.baseTicket.toFixed(2)} <span class="kpi-currency">MXN</span>`);
            setText("sim-base-ticket", `$${STATE.baseTicket.toFixed(2)} MXN`);
            if (kpis.ticket_meta) {
                setHTML("kpi-ticket-meta", `Meta (+25%): <strong class="text-dark">$${Number(kpis.ticket_meta).toFixed(2)} MXN</strong>`);
            }
        }
        if (kpis.top_ancla) setText("kpi-anchor-name", kpis.top_ancla);
        if (kpis.top_ancla_pct != null) {
            setHTML("kpi-anchor-sub", `<i class="fas fa-link small"></i> Presente en ${kpis.top_ancla_pct}% de canastas`);
        }
        if (kpis.combos_activos != null) setText("kpi-combos-activos", String(kpis.combos_activos));
        if (kpis.tasa_adopcion_ia != null) setText("kpi-tasa-adopcion", `${kpis.tasa_adopcion_ia}%`);
        if (kpis.ingreso_ia != null) setText("kpi-ingreso-ia", `$${Number(kpis.ingreso_ia).toFixed(2)}`);
    }

    function renderAll() {
        const visibleRules = STATE.rules.filter(r => r.estado !== "bloqueada");
        renderLiftChart(visibleRules.slice(0, 8));
        renderDoughnutChart();
        renderDashboardRecommendations(visibleRules.slice(0, 8));
        setText("badge-new-combos", `${visibleRules.length} Reglas`);

        renderFullRulesTable(STATE.rules, $("search-rule-input")?.value || "");
        renderScatterChart(visibleRules);
        renderTrainHistory();
        if (!$("view-reglas")?.classList.contains("d-none")) buildNetworkGraph(visibleRules.slice(0, 15));

        setText("meta-rules-count", String(STATE.rules.length));
        setText("meta-train-time", STATE.lastTrainedAt);
        const ms = STATE.lastTrainMs ?? STATE.entrenamientos[0]?.tiempo_ms;
        setText("meta-train-ms", ms != null ? `${Number(ms).toFixed(1)} ms` : "—");

        renderCombosCrudTable();
        updateSimulador();
    }

    function renderLiftChart(rules) {
        const canvas = $("liftBarChart");
        if (!canvas) return;
        const ctx = canvas.getContext("2d");
        const grad = ctx.createLinearGradient(0, 0, 0, 270);
        grad.addColorStop(0, "#BC804D");
        grad.addColorStop(1, "#593720");

        if (liftBarChart) liftBarChart.destroy();
        liftBarChart = new Chart(canvas, {
            type: "bar",
            data: {
                labels: rules.map(r => `${r.ancla} → ${r.complemento}`),
                datasets: [{ label: "Lift", data: rules.map(r => r.lift), backgroundColor: grad, borderRadius: 8 }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { x: { grid: { display: false }, ticks: { maxRotation: 25, font: { size: 10 } } } },
            },
        });
    }

    function renderDoughnutChart() {
        const canvas = $("salesDoughnutChart");
        if (!canvas) return;
        const dist = STATE.distribucionVentas.length
            ? STATE.distribucionVentas
            : [{ categoria: "Sin datos", ingresos: 1 }];

        if (doughnutChart) doughnutChart.destroy();
        doughnutChart = new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: dist.map(d => d.categoria),
                datasets: [{
                    data: dist.map(d => Number(d.ingresos)),
                    backgroundColor: ["#4A2E1B", "#BC804D", "#8C5836", "#DED2C2", "#2C1A10"],
                    borderWidth: 2,
                    borderColor: "#fff",
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "66%",
                plugins: {
                    legend: { position: "bottom", labels: { font: { size: 11 } } },
                    tooltip: { callbacks: { label: c => ` ${c.label}: $${Number(c.raw).toLocaleString("es-MX")} MXN` } },
                },
            },
        });
    }

    function renderScatterChart(rules) {
        const canvas = $("scatterRulesChart");
        if (!canvas) return;
        if (scatterChart) scatterChart.destroy();
        scatterChart = new Chart(canvas, {
            type: "scatter",
            data: {
                datasets: [{
                    label: "Reglas FP-Growth",
                    data: rules.map(r => ({ x: r.soporte, y: r.confianza, rule: `${r.ancla} → ${r.complemento}` })),
                    backgroundColor: "rgba(188,128,77,0.75)",
                    pointRadius: 5,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: c => `${c.raw.rule} (Sop: ${c.raw.x}%, Conf: ${c.raw.y}%)` } },
                },
                scales: { x: { title: { display: true, text: "Soporte (%)" } }, y: { title: { display: true, text: "Confianza (%)" } } },
            },
        });
    }

    function buildNetworkGraph(rules) {
        const container = $("rules-network-graph");
        if (!container || typeof vis === "undefined") return;
        const nodesMap = {};
        const edgesArr = [];

        rules.forEach((r, i) => {
            if (!nodesMap[r.ancla]) {
                nodesMap[r.ancla] = { id: r.ancla, label: r.ancla, color: { background: "#4A2E1B", border: "#BC804D" }, font: { color: "#fff", size: 12 }, shape: "box" };
            }
            if (!nodesMap[r.complemento]) {
                nodesMap[r.complemento] = { id: r.complemento, label: r.complemento, color: { background: "#F3ECE4", border: "#BC804D" }, font: { color: "#4A2E1B", size: 11 }, shape: "ellipse" };
            }
            edgesArr.push({ id: i + 1, from: r.ancla, to: r.complemento, label: `${r.lift}×`, arrows: "to", color: "#BC804D" });
        });

        if (STATE.network) STATE.network.destroy();
        STATE.network = new vis.Network(container, {
            nodes: new vis.DataSet(Object.values(nodesMap)),
            edges: new vis.DataSet(edgesArr),
        }, { physics: { solver: "forceAtlas2Based" } });

        // Clic en un nodo filtra automáticamente la tabla de reglas inferior
        STATE.network.on("click", params => {
            const input = $("search-rule-input");
            if (!input) return;
            input.value = params.nodes.length ? params.nodes[0] : "";
            renderFullRulesTable(STATE.rules, input.value);
        });
    }

    // ── 7. TABLA DE RECOMENDACIONES EN DASHBOARD ──
    function renderDashboardRecommendations(rules) {
        const tbody = $("dashboard-combos-tbody");
        if (!tbody) return;
        if (!rules.length) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted py-4 small">Sin reglas para este filtro.</td></tr>`;
            return;
        }

        tbody.innerHTML = rules.map(r => {
            const existingCombo = STATE.combos.find(c => c.producto_ancla_id === r.producto_ancla_id && c.producto_comp_id === r.producto_comp_id);
            let actionBtn = "";
            if (existingCombo) {
                const isAct = Number(existingCombo.activo) === 1;
                actionBtn = `<button class="combo-toggle-btn ${isAct ? "active" : "inactive"}" onclick="window._toggleComboActive(${existingCombo.id}, ${isAct ? 0 : 1})">
                    ${isAct ? '<i class="fas fa-check me-1"></i>En POS' : "Activar en POS"}
                </button>`;
            } else {
                actionBtn = `<button class="btn btn-sm btn-outline-coffee" onclick="window._openComboFromRule(${r.id})">
                    <i class="fas fa-plus me-1"></i>Crear Combo
                </button>`;
            }

            return `<tr>
                <td class="fw-bold text-coffee"><i class="fas fa-mug-hot text-caramel me-1"></i>${r.ancla}</td>
                <td>${r.complemento}</td>
                <td>${Number(r.confianza).toFixed(1)}%</td>
                <td><span class="badge bg-soft-green text-success fw-bold">${Number(r.lift).toFixed(2)}×</span></td>
                <td><span class="price-anchor me-1">$${Number(r.precio_separado).toFixed(2)}</span><strong class="text-success">$${Number(r.precio_combo).toFixed(2)}</strong></td>
                <td class="text-end">${actionBtn}</td>
            </tr>`;
        }).join("");
    }

    // ── 8. CURACIÓN DE REGLAS IA (APROBAR / BLOQUEAR / HISTORIAL) ──
    function renderFullRulesTable(rules, query = "") {
        const tbody = $("rules-full-tbody");
        if (!tbody) return;
        const q = query.toLowerCase().trim();
        const list = q ? rules.filter(r => r.ancla.toLowerCase().includes(q) || r.complemento.toLowerCase().includes(q)) : rules;

        if (!list.length) {
            tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted py-4 small">Sin coincidencias.</td></tr>`;
            return;
        }

        const estadoBadge = {
            aprobada: '<span class="badge bg-soft-green text-success">Aprobada</span>',
            pendiente: '<span class="badge bg-soft-blue text-primary">Pendiente</span>',
            bloqueada: '<span class="badge bg-danger-subtle text-danger">Bloqueada</span>',
        };

        tbody.innerHTML = list.map(r => `
            <tr>
                <td class="fw-semibold text-coffee">${r.ancla}</td>
                <td>${r.complemento}</td>
                <td>${r.soporte}%</td>
                <td>${r.confianza}%</td>
                <td><span class="badge bg-soft-green text-success fw-bold">${Number(r.lift).toFixed(2)}×</span></td>
                <td>${estadoBadge[r.estado] || r.estado}</td>
                <td class="text-end text-nowrap">
                    ${r.estado !== "bloqueada" ? `
                        <button class="btn btn-sm btn-outline-coffee py-0 px-2 me-1" title="Crear combo desde esta regla" onclick="window._openComboFromRule(${r.id})">
                            <i class="fas fa-plus"></i> Combo
                        </button>
                        <button class="btn btn-sm btn-outline-danger py-0 px-2" title="Bloquear regla" onclick="window._setRuleStatus(${r.id}, 'bloqueada')">
                            <i class="fas fa-ban"></i>
                        </button>
                    ` : `
                        <button class="btn btn-sm btn-outline-success py-0 px-2" onclick="window._setRuleStatus(${r.id}, 'pendiente')">
                            <i class="fas fa-rotate-left me-1"></i>Restaurar
                        </button>
                    `}
                </td>
            </tr>
        `).join("");
    }

    $("search-rule-input") && $("search-rule-input").addEventListener("input", function () {
        renderFullRulesTable(STATE.rules, this.value);
    });

    window._setRuleStatus = async (ruleId, estado) => {
        try {
            await apiJson(`/api/rules/${ruleId}/status`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ estado }),
            });
            showToast(`Regla marcada como "${estado}" en MariaDB`, "success");
            await fetchDashboardData(false);
        } catch (err) {
            showToast(err.message, "error");
        }
    };

    function renderTrainHistory() {
        const tbody = $("train-history-tbody");
        if (!tbody) return;
        if (!STATE.entrenamientos.length) return;
        tbody.innerHTML = STATE.entrenamientos.map(t => `
            <tr>
                <td>#${t.id}</td>
                <td>${t.fecha}</td>
                <td>${t.sucursal_filtro === "all" ? "Todas" : t.sucursal_filtro} / ${TURNO_LABELS[t.turno_filtro] || t.turno_filtro}</td>
                <td>Sop ${(Number(t.min_support) * 100).toFixed(1)}% · Conf ${(Number(t.min_confidence) * 100).toFixed(0)}% · Lift ≥ ${t.min_lift}</td>
                <td><span class="badge bg-soft-green text-success">${Number(t.tiempo_ms).toFixed(1)} ms</span></td>
                <td><strong>${t.total_reglas}</strong> útiles / <span class="text-muted">${t.reglas_descartadas} podadas</span></td>
            </tr>
        `).join("");
    }

    // ── 9. CRUD DE COMBOS REALES EN MARIADB + HABLADOR DE CAJA ──
    async function loadProductsCatalog() {
        try {
            const { products } = await apiJson("/api/pos/products");
            STATE.products = products || [];
            const opts = STATE.products.map(p => `<option value="${p.id}">${p.nombre} ($${Number(p.precio_venta).toFixed(2)})</option>`).join("");
            setHTML("cmb-ancla", opts);
            setHTML("cmb-comp", opts);
        } catch (err) {
            console.error("Error cargando catálogo:", err);
        }
    }

    function updateComboMarginPreview() {
        const aId = parseInt($("cmb-ancla")?.value || 0, 10);
        const cId = parseInt($("cmb-comp")?.value || 0, 10);
        const desc = parseFloat($("cmb-desc")?.value || 10);
        setText("cmb-desc-lbl", `${desc}%`);

        const pa = STATE.products.find(p => p.id === aId);
        const pc = STATE.products.find(p => p.id === cId);
        if (!pa || !pc) return;

        const sep = Number(pa.precio_venta) + Number(pc.precio_venta);
        const combo = sep * (1 - desc / 100);
        const costo = Number(pa.costo_unitario) + Number(pc.costo_unitario);
        const margen = combo > 0 ? ((combo - costo) / combo) * 100 : 0;

        setText("prev-sep", `$${sep.toFixed(2)} MXN`);
        setText("prev-combo", `$${combo.toFixed(2)} MXN`);
        const mEl = $("prev-margen");
        if (mEl) {
            mEl.textContent = `$${costo.toFixed(2)} · Margen ${margen.toFixed(1)}%`;
            mEl.className = margen >= 35 ? "text-success" : (margen > 0 ? "text-warning" : "text-danger");
        }
    }

    ["cmb-ancla", "cmb-comp", "cmb-desc"].forEach(id => {
        $(id) && $(id).addEventListener("input", updateComboMarginPreview);
    });

    $("btn-open-new-combo") && $("btn-open-new-combo").addEventListener("click", () => {
        setText("comboModalTitle", "Nuevo Combo Estratégico");
        $("cmb-id").value = "";
        $("cmb-regla-id").value = "";
        $("cmb-nombre").value = "";
        $("cmb-ancla").disabled = false;
        $("cmb-comp").disabled = false;
        if (STATE.products.length >= 2) {
            $("cmb-ancla").value = STATE.products[0].id;
            $("cmb-comp").value = STATE.products[1].id;
        }
        $("cmb-desc").value = 10;
        $("cmb-sucursal").value = "all";
        $("cmb-turno").value = "all";
        updateComboMarginPreview();
        comboModal.show();
    });

    window._openComboFromRule = (ruleId) => {
        const r = STATE.rules.find(x => x.id === ruleId);
        if (!r) return;
        setText("comboModalTitle", "Crear Combo desde Regla IA");
        $("cmb-id").value = "";
        $("cmb-regla-id").value = r.id;
        $("cmb-nombre").value = `Combo ${r.ancla.slice(0, 20)} + ${r.complemento.slice(0, 20)}`;
        $("cmb-ancla").value = r.producto_ancla_id;
        $("cmb-comp").value = r.producto_comp_id;
        $("cmb-ancla").disabled = false;
        $("cmb-comp").disabled = false;
        $("cmb-desc").value = r.lift >= 2.0 ? 12 : 10;
        $("cmb-sucursal").value = $("location-select")?.value || "all";
        $("cmb-turno").value = STATE.timeSlot || "all";
        updateComboMarginPreview();
        comboModal.show();
    };

    window._editCombo = (comboId) => {
        const c = STATE.combos.find(x => x.id === comboId);
        if (!c) return;
        setText("comboModalTitle", `Editar Combo #${c.id}`);
        $("cmb-id").value = c.id;
        $("cmb-regla-id").value = c.regla_id || "";
        $("cmb-nombre").value = c.nombre_combo;
        $("cmb-ancla").value = c.producto_ancla_id;
        $("cmb-comp").value = c.producto_comp_id;
        $("cmb-ancla").disabled = true;
        $("cmb-comp").disabled = true;
        $("cmb-desc").value = Math.round(Number(c.descuento_pct));
        $("cmb-sucursal").value = c.sucursal;
        $("cmb-turno").value = c.turno;
        updateComboMarginPreview();
        comboModal.show();
    };

    $("combo-form") && $("combo-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const id = $("cmb-id").value;
        const body = {
            nombre_combo: $("cmb-nombre").value.trim(),
            regla_id: $("cmb-regla-id").value ? parseInt($("cmb-regla-id").value, 10) : null,
            producto_ancla_id: parseInt($("cmb-ancla").value, 10),
            producto_comp_id: parseInt($("cmb-comp").value, 10),
            descuento_pct: parseFloat($("cmb-desc").value),
            sucursal: $("cmb-sucursal").value,
            turno: $("cmb-turno").value,
        };

        try {
            if (id) {
                const data = await apiJson(`/api/combos/${id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
                STATE.combos = data.combos;
                showToast("Combo actualizado en MariaDB", "success");
            } else {
                const data = await apiJson("/api/combos", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
                STATE.combos = data.combos;
                showToast("Combo creado y publicado en Caja POS", "success");
            }
            comboModal.hide();
            await fetchDashboardData(false);
        } catch (err) {
            showToast(err.message, "error");
        }
    });

    window._toggleComboActive = async (comboId, nextActive) => {
        try {
            const data = await apiJson(`/api/combos/${comboId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ activo: nextActive }),
            });
            STATE.combos = data.combos;
            showToast(nextActive ? "Combo activado en Caja POS" : "Combo pausado", "success");
            renderAll();
            setText("kpi-combos-activos", String(STATE.combos.filter(c => Number(c.activo) === 1).length));
        } catch (err) {
            showToast(err.message, "error");
        }
    };

    window._deleteCombo = async (comboId) => {
        if (!confirm("¿Eliminar permanentemente este combo de MariaDB?")) return;
        try {
            const data = await apiJson(`/api/combos/${comboId}`, { method: "DELETE" });
            STATE.combos = data.combos;
            showToast("Combo eliminado de MariaDB", "warning");
            await fetchDashboardData(false);
        } catch (err) {
            showToast(err.message, "error");
        }
    };

    window._openHablador = (comboId) => {
        const c = STATE.combos.find(x => x.id === comboId);
        if (!c) return;
        const ahorro = Math.max(Number(c.precio_separado) - Number(c.precio_combo), 0);
        setText("hab-titulo", c.nombre_combo);
        setText("hab-productos", `${c.ancla} + ${c.complemento}`);
        setText("hab-regular", `$${Number(c.precio_separado).toFixed(2)} MXN`);
        setText("hab-combo", `$${Number(c.precio_combo).toFixed(2)}`);
        setText("hab-ahorro", `¡Ahorras $${ahorro.toFixed(2)} MXN (${Number(c.descuento_pct).toFixed(0)}% OFF)!`);
        setText("hab-vigencia", `Sucursal: ${c.sucursal === "all" ? "Todas" : c.sucursal} · Turno: ${TURNO_LABELS[c.turno] || c.turno}`);
        habladorModal.show();
    };

    function renderCombosCrudTable() {
        const tbody = $("combo-simulator-tbody");
        if (!tbody) return;
        if (!STATE.combos.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4 small">No hay combos creados aún. Pulsa "+ Nuevo Combo".</td></tr>`;
            return;
        }

        tbody.innerHTML = STATE.combos.map(c => {
            const act = Number(c.activo) === 1;
            const sucTxt = c.sucursal === "all" ? "Nacional" : c.sucursal;
            const turnTxt = TURNO_LABELS[c.turno] || c.turno;
            return `
                <tr>
                    <td>
                        <div class="fw-bold text-coffee small">${c.nombre_combo}</div>
                        <div class="text-muted" style="font-size:.73rem">${c.ancla} + ${c.complemento}</div>
                        <span class="badge bg-soft-brown text-coffee" style="font-size:.68rem">${sucTxt} · ${turnTxt}</span>
                    </td>
                    <td>
                        <div><span class="price-anchor">$${Number(c.precio_separado).toFixed(2)}</span> → <span class="price-combo-ia">$${Number(c.precio_combo).toFixed(2)}</span></div>
                        <div class="text-muted" style="font-size:.72rem">Desc. ${Number(c.descuento_pct).toFixed(0)}% · Lift ${Number(c.lift).toFixed(1)}×</div>
                    </td>
                    <td>
                        <span class="badge ${Number(c.margen_pct) >= 40 ? "bg-soft-green text-success" : "bg-warning-subtle text-dark"} fw-bold">
                            ${Number(c.margen_pct).toFixed(1)}%
                        </span>
                    </td>
                    <td class="small">
                        <strong>${c.aceptados || 0}/${c.mostrados || 0}</strong>
                        <div class="text-muted" style="font-size:.72rem">${Number(c.conversion_real || 0).toFixed(1)}% conv.</div>
                    </td>
                    <td class="text-end text-nowrap">
                        <button class="combo-toggle-btn ${act ? "active" : "inactive"} me-1" onclick="window._toggleComboActive(${c.id}, ${act ? 0 : 1})">
                            ${act ? "Activo" : "Pausado"}
                        </button>
                        <button class="btn btn-sm btn-outline-secondary py-0 px-1" title="Hablador de caja" onclick="window._openHablador(${c.id})"><i class="fas fa-tag"></i></button>
                        <button class="btn btn-sm btn-outline-primary py-0 px-1" title="Editar" onclick="window._editCombo(${c.id})"><i class="fas fa-pen"></i></button>
                        <button class="btn btn-sm btn-outline-danger py-0 px-1" title="Eliminar" onclick="window._deleteCombo(${c.id})"><i class="fas fa-trash"></i></button>
                    </td>
                </tr>
            `;
        }).join("");
    }

    async function updateSimulador() {
        const activos = STATE.combos.filter(c => Number(c.activo) === 1);
        const count = activos.length;
        setText("kpi-combos-activos", String(count));
        setText("active-combos-count", String(count));
        setHTML("active-combos-badge", `${count} Activo${count !== 1 ? "s" : ""}`);

        const avgMargin = count
            ? activos.reduce((acc, c) => acc + Number(c.margen_pct || 0), 0) / count
            : 0;
        setText("sim-avg-margin", `${avgMargin.toFixed(1)}%`);

        const convRate = parseFloat($("slider-sim-conv")?.value || 35) / 100;
        const activePayload = activos.map(c => ({
            id: c.id,
            combo_price: Number(c.precio_combo),
            confidence: Number(c.confianza || 60) / 100,
        }));

        try {
            const data = await apiJson("/api/simulate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    active_combos: activePayload,
                    base_ticket: STATE.baseTicket,
                    conversion_rate: convRate,
                }),
            });
            setText("sim-projected-ticket", `$${Number(data.projected_ticket).toFixed(2)}`);
            setText("sim-ticket-diff", `+$${Number(data.total_gain).toFixed(2)} (+${Number(data.pct_increase).toFixed(1)}%) sobre base`);
        } catch (err) {
            console.error("Error simulador:", err);
        }
    }

    // ── 10. GESTIÓN DE USUARIOS (ADMIN / EMPLEADO) ──
    async function loadUsers() {
        const tbody = $("users-tbody");
        if (!tbody) return;
        try {
            const { users } = await apiJson("/api/users");
            tbody.innerHTML = users.map(u => {
                const act = Number(u.activo) === 1;
                const isSelf = STATE.user && STATE.user.id === u.id;
                return `
                    <tr>
                        <td class="fw-semibold text-coffee">${u.nombre}</td>
                        <td><code>${u.email}</code></td>
                        <td><span class="badge ${u.rol === "admin" ? "bg-soft-brown text-coffee" : "bg-soft-blue text-primary"}">${u.rol.toUpperCase()}</span></td>
                        <td>${u.sucursal === "all" ? "Todas" : u.sucursal}</td>
                        <td><span class="badge ${act ? "bg-soft-green text-success" : "bg-danger-subtle text-danger"}">${act ? "Activo" : "Inactivo"}</span></td>
                        <td class="text-end">
                            ${isSelf ? '<span class="text-muted">Sesión actual</span>' : `
                                <button class="btn btn-sm ${act ? "btn-outline-danger" : "btn-outline-success"} py-0 px-2" onclick="window._toggleUser(${u.id})">
                                    ${act ? "Desactivar" : "Reactivar"}
                                </button>
                            `}
                        </td>
                    </tr>
                `;
            }).join("");
        } catch (err) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-danger small">${err.message}</td></tr>`;
        }
    }

    $("btn-open-new-user") && $("btn-open-new-user").addEventListener("click", () => {
        $("user-form").reset();
        userModal.show();
    });

    $("user-form") && $("user-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
            await apiJson("/api/users", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    nombre: $("usr-nombre").value.trim(),
                    email: $("usr-email").value.trim(),
                    password: $("usr-password").value,
                    rol: $("usr-rol").value,
                    sucursal: $("usr-sucursal").value,
                }),
            });
            userModal.hide();
            showToast("Usuario registrado en MariaDB", "success");
            loadUsers();
        } catch (err) {
            showToast(err.message, "error");
        }
    });

    window._toggleUser = async (userId) => {
        try {
            await apiJson(`/api/users/${userId}/toggle`, { method: "PATCH" });
            showToast("Estado del usuario actualizado", "success");
            loadUsers();
        } catch (err) {
            showToast(err.message, "error");
        }
    };

    // ── 11. PDF, TOUR GUIADO, BACKUP SQL Y ESTADO API ──
    $("btn-export-pdf") && $("btn-export-pdf").addEventListener("click", () => {
        const activos = STATE.combos.filter(c => Number(c.activo) === 1);
        if (!activos.length) return showToast("Activa al menos un combo primero.", "warning");

        setHTML("pdf-combos-tbody", activos.map(c => `<tr>
            <td><strong>${c.nombre_combo}</strong><br><small>${c.ancla} + ${c.complemento}</small></td>
            <td>${c.sucursal === "all" ? "Todas" : c.sucursal} / ${TURNO_LABELS[c.turno] || c.turno}</td>
            <td>$${Number(c.precio_separado).toFixed(2)}</td>
            <td><strong>$${Number(c.precio_combo).toFixed(2)}</strong></td>
            <td>${Number(c.descuento_pct).toFixed(0)}%</td>
            <td>${Number(c.margen_pct).toFixed(1)}%</td>
        </tr>`).join(""));
        setText("pdf-fecha-gen", `Generado: ${new Date().toLocaleString("es-MX")} · MariaDB`);

        const panel = $("pdf-export-panel");
        panel.classList.remove("d-none");
        html2pdf().set({ margin: 12, filename: "BrewSense_Dictamen_Combos.pdf", html2canvas: { scale: 2 }, jsPDF: { format: "a4" } })
            .from(panel).save().then(() => panel.classList.add("d-none"));
    });

    $("btn-tour") && $("btn-tour").addEventListener("click", () => {
        if (!window.driver) return;
        showView("view-dashboard");
        window.driver.js.driver({
            showProgress: true,
            nextBtnText: "Siguiente →",
            prevBtnText: "← Anterior",
            doneBtnText: "¡Listo!",
            steps: [
                { element: "#kpi-card-tickets", popover: { title: "Datos Reales en MariaDB", description: "Muestra tanto el histórico POS como las ventas nuevas cobradas desde la Caja POS." } },
                { element: "#kpi-card-combos", popover: { title: "Adopción IA en Vivo", description: "Mide el porcentaje real de clientes que aceptaron combos sugeridos en caja." } },
                { element: "#btn-apply-params", popover: { title: "Motor FP-Growth", description: "Reentrena el algoritmo sobre MariaDB y registra el tiempo de cómputo." } },
                { element: "#nav-combos", popover: { title: "Gestión de Combos (CRUD)", description: "Crea, edita y valida márgenes antes de publicar en la Caja POS." } },
                { element: "#nav-pos-link", popover: { title: "Caja POS con IA", description: "Abre el punto de venta donde los empleados reciben sugerencias automáticas." } },
            ],
        }).drive();
    });

    $("btn-backup-db") && $("btn-backup-db").addEventListener("click", async () => {
        btnLoading("btn-backup-db", true);
        try {
            const res = await fetch("/api/backup-sql");
            if (!res.ok) throw new Error("No se pudo generar el respaldo");
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `backup_brewsense_${new Date().toISOString().slice(0, 10)}.sql`;
            a.click();
            URL.revokeObjectURL(url);
            setText("last-backup-time", new Date().toLocaleString("es-MX"));
            showToast("Respaldo SQL descargado desde MariaDB", "success");
        } catch (err) {
            showToast(err.message, "error");
        } finally {
            btnLoading("btn-backup-db", false, '<i class="fas fa-download me-2"></i>Generar Respaldo SQL Ahora');
        }
    });

    async function checkApiStatus() {
        const badge = $("cfg-api-status");
        if (!badge) return;
        try {
            const data = await apiJson("/api/status");
            badge.textContent = `Conectado · ${data.db_status}`;
            badge.className = "badge ok";
        } catch {
            badge.textContent = "Backend desconectado";
            badge.className = "badge error";
        }
    }

    // ── 12. ARRANQUE INICIAL ──
    (async function init() {
        comboModal = new bootstrap.Modal($("comboModal"));
        habladorModal = new bootstrap.Modal($("habladorModal"));
        userModal = new bootstrap.Modal($("userModal"));

        await loadSessionUser();
        await Promise.all([loadProductsCatalog(), checkApiStatus()]);
        try {
            await fetchDashboardData(false);
            showToast(`Conectado a MariaDB :3307 · ${STATE.rules.length} reglas activas`, "success");
        } catch (err) {
            showToast(err.message, "warning");
        }
    })();
});