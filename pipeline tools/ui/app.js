/*
 * Stage 6 Portfolio Dashboard — vanilla SPA.
 *
 * Loads JSON files from `./data/` (override with `?dataRoot=…` for local dev).
 * Hash-based routing: #/, #/positions, #/ticker/{T}, #/forecasts, #/history,
 * #/snapshots. Every view is fail-soft — a missing JSON renders an empty card,
 * never a thrown exception.
 */

(function () {
  "use strict";

  // ============ Config / DOM refs ============

  const params = new URLSearchParams(window.location.search);
  const DATA_ROOT = (params.get("dataRoot") || "./data").replace(/\/+$/, "");

  const app = document.getElementById("app");
  const headerMeta = document.getElementById("header-meta");
  const qualityBanner = document.getElementById("quality-banner");
  const navLinks = Array.from(document.querySelectorAll(".nav a"));

  let chartInstance = null;
  let dataQualityLoaded = null;

  // ============ Data layer ============

  async function fetchJson(relPath) {
    try {
      const res = await fetch(`${DATA_ROOT}/${relPath}`, { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      console.warn(`fetchJson ${relPath} failed`, e);
      return null;
    }
  }

  // ============ Formatters ============

  const fmtMoney = (v, opts = {}) => {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v.toLocaleString("en-US", {
      style: "currency", currency: "USD",
      minimumFractionDigits: opts.dp ?? 2, maximumFractionDigits: opts.dp ?? 2,
    });
  };
  const fmtPct = (v, dp = 2) => {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return `${(v * 100).toFixed(dp)}%`;
  };
  const fmtPctRaw = (v, dp = 2) => {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return `${v.toFixed(dp)}%`;
  };
  const fmtNum = (v, dp = 2) => {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: dp, maximumFractionDigits: dp,
    });
  };
  const fmtInt = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US"));
  const fmtDate = (s) => (s ? String(s).slice(0, 10) : "—");
  const fmtTs = (s) => {
    if (!s) return "—";
    try {
      return new Date(s).toISOString().replace("T", " ").slice(0, 19) + "Z";
    } catch {
      return String(s);
    }
  };
  const signClass = (v) => (v === null || v === undefined ? "" : v >= 0 ? "pos" : "neg");

  const el = (tag, attrs = {}, children = []) => {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function")
        node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v);
    });
    (Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  };

  function renderEmpty(title, msg) {
    const wrap = document.createElement("div");
    wrap.className = "empty";
    wrap.innerHTML = `<h3>${title}</h3><div>${msg}</div>`;
    return wrap;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ============ Data quality banner ============

  async function ensureDataQuality() {
    if (dataQualityLoaded !== null) return dataQualityLoaded;
    dataQualityLoaded = (await fetchJson("data_quality.json")) || {};
    renderQualityBanner(dataQualityLoaded);
    renderHeaderMeta(dataQualityLoaded);
    return dataQualityLoaded;
  }

  function renderQualityBanner(dq) {
    const live = dq.live || {};
    const stale = Object.entries(live).filter(([, v]) => v === true).map(([k]) => k);
    if (!stale.length) {
      qualityBanner.classList.add("hidden");
      return;
    }
    qualityBanner.classList.remove("hidden");
    qualityBanner.textContent =
      "⚠ Live data is stale: " + stale.map((s) => s.replace(/_/g, " ")).join(", ") +
      ". The dashboard is showing the most recent cached values.";
  }

  function renderHeaderMeta(dq) {
    const parts = [];
    if (dq.snapshot_id) parts.push(`snapshot ${dq.snapshot_id}`);
    if (dq.as_of) parts.push(`as-of ${fmtTs(dq.as_of)}`);
    headerMeta.textContent = parts.join("  ·  ");
  }

  // ============ Router ============

  function setActiveNav(route) {
    navLinks.forEach((a) =>
      a.classList.toggle("active", a.getAttribute("data-route") === route)
    );
  }

  function destroyChart() {
    if (chartInstance) {
      chartInstance.destroy();
      chartInstance = null;
    }
  }

  async function route() {
    destroyChart();
    await ensureDataQuality();
    const hash = window.location.hash || "#/";
    const parts = hash.replace(/^#\//, "").split("/").filter(Boolean);
    const route = parts[0] || "overview";

    if (route === "" || route === "overview") {
      setActiveNav("overview");
      await renderOverview();
    } else if (route === "positions") {
      setActiveNav("positions");
      await renderPositions();
    } else if (route === "ticker" && parts[1]) {
      setActiveNav("positions");
      await renderTicker(parts[1]);
    } else if (route === "forecasts") {
      setActiveNav("forecasts");
      await renderForecasts();
    } else if (route === "history") {
      setActiveNav("history");
      await renderHistory();
    } else if (route === "snapshots") {
      setActiveNav("snapshots");
      await renderSnapshots();
    } else {
      setActiveNav("");
      app.replaceChildren(renderEmpty("Unknown view", `Hash route: ${hash}`));
    }
  }

  // ============ Overview ============

  async function renderOverview() {
    app.replaceChildren(el("div", { class: "loading" }, "Loading overview…"));

    const [overview, valueHist, benchHist] = await Promise.all([
      fetchJson("portfolio_overview.json"),
      fetchJson("portfolio_value_history.json"),
      fetchJson("benchmark_history.json"),
    ]);

    if (!overview) {
      app.replaceChildren(
        renderEmpty(
          "No Stage 6 outputs yet",
          "Run the <b>Stage 6 Consolidation</b> workflow to populate the dashboard."
        )
      );
      return;
    }

    const container = document.createElement("div");

    // Title
    container.appendChild(el("h1", { class: "view-title" }, "Portfolio Overview"));

    // Top-line cards
    const acct = overview.account || {};
    const perf = overview.performance || {};
    const cards = el("div", { class: "cards" });
    cards.append(
      makeCard("Equity", fmtMoney(acct.equity), acct.source === "stale_cache" ? "cached" : "alpaca"),
      makeCard("Cash", fmtMoney(acct.cash)),
      makeCard("Buying power", fmtMoney(acct.buying_power)),
      makeCard("Total return", fmtPct(perf.total_return_pct), `${perf.since_inception_days ?? "—"} days`, signClass(perf.total_return_pct)),
      makeCard("YTD", fmtPct(perf.ytd_return), `SPY: ${fmtPct(perf.spy_ytd)}`, signClass(perf.ytd_return)),
      makeCard("Max drawdown", fmtPct(perf.max_drawdown_pct), `now: ${fmtPct(perf.current_drawdown_pct)}`, signClass(perf.max_drawdown_pct))
    );
    container.appendChild(cards);

    // Returns table
    container.appendChild(el("h2", { class: "section-title" }, "Returns vs benchmark"));
    container.appendChild(returnsTable(perf));

    // Chart
    container.appendChild(el("h2", { class: "section-title" }, "Value vs SPY (normalised)"));
    const chartWrap = el("div", { class: "chart-wrap" });
    const canvas = el("canvas", { id: "value-chart" });
    chartWrap.appendChild(canvas);
    container.appendChild(chartWrap);

    // Pipeline run + top positions
    container.appendChild(el("h2", { class: "section-title" }, "Latest pipeline run"));
    container.appendChild(pipelineRunPanel(overview.pipeline_run, overview.target_portfolio_source_file));

    const positions = overview.positions || [];
    container.appendChild(el("h2", { class: "section-title" }, `Top positions (${positions.length})`));
    container.appendChild(positionsTable(positions.slice(0, 10)));

    app.replaceChildren(container);
    drawValueChart(canvas, valueHist, benchHist);
  }

  function makeCard(label, value, sub, valueClass) {
    return el("div", { class: "card" }, [
      el("div", { class: "label" }, label),
      el("div", { class: "value " + (valueClass || "") }, String(value)),
      sub !== undefined ? el("div", { class: "sub" }, String(sub)) : null,
    ]);
  }

  function returnsTable(perf) {
    const rows = [
      ["1m", perf.m1_return, perf.spy_m1],
      ["3m", perf.m3_return, perf.spy_m3],
      ["6m", perf.m6_return, perf.spy_m6],
      ["12m", perf.m12_return, perf.spy_m12],
      ["MTD", perf.mtd_return, perf.spy_mtd],
      ["YTD", perf.ytd_return, perf.spy_ytd],
    ];
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(
      el("thead", {}, el("tr", {}, [
        el("th", {}, "Horizon"),
        el("th", { class: "num" }, "Portfolio"),
        el("th", { class: "num" }, "SPY"),
        el("th", { class: "num" }, "Excess"),
      ]))
    );
    const tbody = el("tbody");
    rows.forEach(([label, p, spy]) => {
      const excess = (p !== null && spy !== null && p !== undefined && spy !== undefined) ? p - spy : null;
      tbody.appendChild(el("tr", {}, [
        el("td", {}, label),
        el("td", { class: "num " + signClass(p) }, fmtPct(p)),
        el("td", { class: "num " + signClass(spy) }, fmtPct(spy)),
        el("td", { class: "num " + signClass(excess) }, fmtPct(excess)),
      ]));
    });
    t.appendChild(tbody);
    wrap.appendChild(t);
    return wrap;
  }

  function pipelineRunPanel(run, sourceFile) {
    if (!run) return renderEmpty("No pipeline run", "Stage 4 consolidation not present in the latest snapshot.");
    const panel = el("div", { class: "panel" });
    panel.append(
      el("div", { class: "cards", style: "margin-bottom:0" }, [
        makeCard("Consolidation date", fmtDate(run.latest_consolidation_date)),
        makeCard("Reconciled", fmtDate(run.latest_reconciliation_date)),
        makeCard("Status", run.status || "—"),
        makeCard("Positions", fmtInt(run.positions_count)),
      ])
    );
    return panel;
  }

  function drawValueChart(canvas, valueHist, benchHist) {
    if (typeof Chart === "undefined") return;
    const vSeries = ((valueHist || {}).series) || [];
    const bSeries = ((benchHist || {}).series) || [];
    if (!vSeries.length && !bSeries.length) {
      const ctx = canvas.parentNode;
      ctx.replaceChildren(renderEmpty("No price history yet", "Stage 6 has no Alpaca portfolio history to chart."));
      return;
    }
    const labels = vSeries.length ? vSeries.map((p) => p.date) : bSeries.map((p) => p.date);
    chartInstance = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Portfolio (normalised)",
            data: vSeries.map((p) => p.normalised),
            borderColor: "#2c5282",
            backgroundColor: "rgba(44, 82, 130, 0.08)",
            tension: 0.15,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: `${(benchHist || {}).symbol || "SPY"} (normalised)`,
            data: bSeries.map((p) => p.normalised),
            borderColor: "#b7791f",
            tension: 0.15,
            pointRadius: 0,
            borderWidth: 2,
            borderDash: [4, 4],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: { legend: { position: "top" } },
        scales: {
          x: { ticks: { maxTicksLimit: 12 } },
          y: { ticks: { callback: (v) => (v * 100 - 100).toFixed(1) + "%" } },
        },
      },
    });
  }

  // ============ Positions table (shared) ============

  let positionsSortKey = "target_allocation_pct";
  let positionsSortDir = "desc";

  function positionsTable(positions) {
    const wrap = el("div", { class: "table-wrap" });
    if (!positions || !positions.length) {
      wrap.appendChild(renderEmpty("No positions", "The latest target portfolio has no positions."));
      return wrap;
    }
    const cols = [
      { key: "ticker", label: "Ticker", num: false, cls: "ticker" },
      { key: "sector", label: "Sector", num: false },
      { key: "status", label: "Status", num: false },
      { key: "target_allocation_pct", label: "Target %", num: true, render: (v) => fmtPctRaw(v) },
      { key: "actual_allocation_pct", label: "Actual %", num: true, render: (v) => fmtPctRaw(v) },
      { key: "drift_pct", label: "Drift", num: true, render: (v) => fmtPctRaw(v), sign: true },
      { key: "qty", label: "Qty", num: true, render: (v) => fmtNum(v, 2) },
      { key: "market_value", label: "MV", num: true, render: (v) => fmtMoney(v, { dp: 0 }) },
      { key: "unrealized_pl", label: "Unrealised P&L", num: true, render: (v) => fmtMoney(v, { dp: 0 }), sign: true },
      { key: "conviction", label: "Conviction", num: false },
      { key: "expected_return_12m", label: "Exp 12m", num: true, render: (v) => fmtPct(v), sign: true },
    ];

    const sorted = sortRows(positions, positionsSortKey, positionsSortDir);
    const table = el("table");
    table.appendChild(el("thead", {}, el("tr", {}, cols.map((c) => {
      const cls = ["sortable", c.num ? "num" : ""].filter(Boolean).join(" ");
      const th = el("th", { class: cls + (c.key === positionsSortKey ? ` sort-${positionsSortDir}` : "") }, c.label);
      th.addEventListener("click", () => {
        if (positionsSortKey === c.key) {
          positionsSortDir = positionsSortDir === "asc" ? "desc" : "asc";
        } else {
          positionsSortKey = c.key;
          positionsSortDir = c.num ? "desc" : "asc";
        }
        route();
      });
      return th;
    }))));

    const tbody = el("tbody");
    sorted.forEach((p) => {
      const tr = el("tr", { class: "row-link" });
      tr.addEventListener("click", () => {
        window.location.hash = `#/ticker/${encodeURIComponent(p.ticker)}`;
      });
      cols.forEach((c) => {
        const raw = p[c.key];
        const text = c.render ? c.render(raw) : (raw ?? "—");
        const cls = [c.num ? "num" : "", c.cls || "", c.sign ? signClass(raw) : ""].filter(Boolean).join(" ");
        tr.appendChild(el("td", { class: cls }, String(text)));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function sortRows(rows, key, dir) {
    const sign = dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * sign;
      return String(av).localeCompare(String(bv)) * sign;
    });
  }

  async function renderPositions() {
    app.replaceChildren(el("div", { class: "loading" }, "Loading positions…"));
    const overview = await fetchJson("portfolio_overview.json");
    if (!overview) {
      app.replaceChildren(renderEmpty("No portfolio_overview.json", "Run Stage 6 first."));
      return;
    }
    const container = el("div");
    container.appendChild(el("h1", { class: "view-title" }, `Positions (${(overview.positions || []).length})`));
    container.appendChild(positionsTable(overview.positions || []));
    app.replaceChildren(container);
  }

  // ============ Per-ticker ============

  async function renderTicker(ticker) {
    app.replaceChildren(el("div", { class: "loading" }, `Loading ${ticker}…`));
    const dossier = await fetchJson(`per_ticker_dossier/${encodeURIComponent(ticker)}.json`);
    const container = el("div");

    if (!dossier) {
      container.appendChild(el("h1", { class: "view-title" }, ticker));
      container.appendChild(renderEmpty("No dossier", `Per-ticker file missing for ${ticker}. <a href="#/positions">Back to positions</a>`));
      app.replaceChildren(container);
      return;
    }

    const header = el("div", { class: "ticker-header" }, [
      el("h1", {}, dossier.ticker || ticker),
      el("div", { class: "name" }, dossier.company_name || ""),
    ]);
    container.appendChild(header);

    const tabs = el("div", { class: "tabs" });
    const panels = el("div", { class: "tab-panels" });

    const tabSpec = [
      { id: "scores", label: "Scores", render: () => renderScoresTab(dossier.stage1_scores) },
      { id: "debate", label: "Debate", render: () => renderDebateTab(dossier.stage2_debate) },
      { id: "scenarios", label: "Scenarios & valuation", render: () => renderScenariosTab(dossier.stage3_scenarios_and_valuation) },
      { id: "status", label: "Portfolio status", render: () => renderStatusTab(dossier.stage4_portfolio_status, dossier.live_position) },
      { id: "history", label: "History", render: () => renderTickerHistoryTab(dossier.history) },
    ];

    tabSpec.forEach((spec, i) => {
      const btn = el("button", { "data-tab": spec.id }, spec.label);
      const panel = el("div", { class: "tab-panel", "data-tab": spec.id });
      panel.appendChild(spec.render());
      btn.addEventListener("click", () => activateTab(spec.id));
      tabs.appendChild(btn);
      panels.appendChild(panel);
      if (i === 0) {
        btn.classList.add("active");
        panel.classList.add("active");
      }
    });

    function activateTab(id) {
      tabs.querySelectorAll("button").forEach((b) =>
        b.classList.toggle("active", b.getAttribute("data-tab") === id)
      );
      panels.querySelectorAll(".tab-panel").forEach((p) =>
        p.classList.toggle("active", p.getAttribute("data-tab") === id)
      );
    }

    container.append(tabs, panels);
    app.replaceChildren(container);
  }

  function renderScoresTab(s) {
    if (!s) return renderEmpty("No scores", "Stage 1 data missing for this ticker.");
    const wrap = el("div");
    wrap.appendChild(el("div", { class: "cards" }, [
      makeCard("Composite", fmtNum(s.composite_score, 1), `rank ${fmtInt(s.composite_rank)}`),
      makeCard("Financial", fmtNum(s.financial_score, 1)),
      makeCard("Professional", fmtNum(s.professional_score, 1)),
      makeCard("News sentiment", fmtNum(s.news_sentiment_score, 1)),
      makeCard("Qualified", s.qualified === true ? "yes" : s.qualified === false ? "no" : "—"),
    ]));
    if (s.financial_subscores) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Financial sub-scores"));
      wrap.appendChild(subscoreTable(s.financial_subscores));
    }
    if (s.professional_subscores) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Professional sub-scores"));
      wrap.appendChild(subscoreTable(s.professional_subscores));
    }
    if (s.news_sentiment_subscores) {
      wrap.appendChild(el("h2", { class: "section-title" }, "News sentiment sub-scores"));
      wrap.appendChild(subscoreTable(s.news_sentiment_subscores));
    }
    if (s.disqualifier_flags) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Disqualifier check"));
      wrap.appendChild(el("pre", { class: "json" }, JSON.stringify(s.disqualifier_flags, null, 2)));
    }
    return wrap;
  }

  function subscoreTable(obj) {
    const rows = Object.entries(obj || {});
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Component"),
      el("th", { class: "num" }, "Score"),
    ])));
    const body = el("tbody");
    rows.forEach(([k, v]) => {
      body.appendChild(el("tr", {}, [
        el("td", {}, k.replace(/_/g, " ")),
        el("td", { class: "num" }, fmtNum(v, 1)),
      ]));
    });
    t.appendChild(body);
    wrap.appendChild(t);
    return wrap;
  }

  function renderDebateTab(d) {
    if (!d) return renderEmpty("No debate", "Stage 2 data missing for this ticker.");
    const wrap = el("div");
    const synthCard = el("div", { class: "cards" }, [
      makeCard("Synthesis score", fmtInt(d.synthesis_score)),
      makeCard("Sentiment", d.synthesis_categorical || "—"),
      makeCard("Confidence", d.synthesis_score_confidence || "—"),
    ]);
    wrap.appendChild(synthCard);

    const blocks = [
      ["Synthesis", d.synthesis, d.synthesis_date],
      ["Bull case", d.bull_case, d.bull_case_date],
      ["Bear case", d.bear_case, d.bear_case_date],
      ["Bull rebuttal", d.bull_rebuttal, d.bull_rebuttal_date],
      ["Bear rebuttal", d.bear_rebuttal, d.bear_rebuttal_date],
      ["Finance research", d.finance_research_report, d.finance_research_report_date],
      ["News research", d.news_research_report, d.news_research_report_date],
      ["Environment research", d.environment_research_report, d.environment_research_report_date],
    ];
    blocks.forEach(([label, body, date]) => {
      if (!body) return;
      const det = el("details", { class: "collapse" }, [
        el("summary", {}, `${label} (${fmtDate(date)})`),
        el("div", { class: "body" }, body),
      ]);
      wrap.appendChild(det);
    });
    return wrap;
  }

  function renderScenariosTab(s) {
    if (!s) return renderEmpty("No scenarios", "Stage 3 data missing for this ticker.");
    const wrap = el("div");

    // Cards
    const probs = s.scenario_probabilities || {};
    wrap.appendChild(el("div", { class: "cards" }, [
      makeCard("Conviction", s.conviction || "—"),
      makeCard("P(Bull)", fmtPct(probs.bull)),
      makeCard("P(Base)", fmtPct(probs.base)),
      makeCard("P(Bear)", fmtPct(probs.bear)),
      makeCard("Upside 12m", fmtPct(s.upside_return_12m), null, signClass(s.upside_return_12m)),
      makeCard("Base 12m", fmtPct(s.base_return_12m), null, signClass(s.base_return_12m)),
      makeCard("Downside 12m", fmtPct(s.downside_return_12m), null, signClass(s.downside_return_12m)),
    ]));

    if (s.thesis_summary) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Thesis"));
      wrap.appendChild(el("div", { class: "panel" }, s.thesis_summary));
    }

    // Price targets table
    const pt = s.price_targets || {};
    if (pt.bull || pt.base || pt.bear) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Price targets"));
      const t = el("table");
      t.appendChild(el("thead", {}, el("tr", {}, [
        el("th", {}, "Scenario"),
        ...["1m", "3m", "6m", "12m"].map((h) => el("th", { class: "num" }, h)),
      ])));
      const tb = el("tbody");
      ["bull", "base", "bear"].forEach((k) => {
        const row = pt[k] || {};
        tb.appendChild(el("tr", {}, [
          el("td", {}, k),
          ...["1m", "3m", "6m", "12m"].map((h) => el("td", { class: "num" }, fmtMoney(row[h]))),
        ]));
      });
      t.appendChild(tb);
      const wrap2 = el("div", { class: "table-wrap" });
      wrap2.appendChild(t);
      wrap.appendChild(wrap2);
    }

    if (s.key_invalidation_triggers && s.key_invalidation_triggers.length) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Invalidation triggers"));
      const list = el("ul");
      s.key_invalidation_triggers.forEach((t) => list.appendChild(el("li", {}, t)));
      wrap.appendChild(el("div", { class: "panel" }, list));
    }

    // Valuation metrics (collapsible JSON)
    if (s.valuation_metrics) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Valuation metrics"));
      wrap.appendChild(el("details", { class: "collapse" }, [
        el("summary", {}, "Full valuation_metrics JSON"),
        el("div", { class: "body" }, el("pre", { class: "json" }, JSON.stringify(s.valuation_metrics, null, 2))),
      ]));
    }

    // Consolidation text
    if (s.consolidation) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Consolidation text"));
      wrap.appendChild(el("details", { class: "collapse" }, [
        el("summary", {}, `Consolidation (${fmtDate(s.consolidation_date)})`),
        el("div", { class: "body" }, s.consolidation),
      ]));
    }
    return wrap;
  }

  function renderStatusTab(status, live) {
    const wrap = el("div");
    wrap.appendChild(el("h2", { class: "section-title" }, "Stage 4 status"));
    if (!status) {
      wrap.appendChild(renderEmpty("Not in latest target portfolio", ""));
    } else {
      wrap.appendChild(el("div", { class: "cards" }, [
        makeCard("Status", status.status || "—"),
        makeCard("Target %", fmtPctRaw(status.target_allocation_pct)),
        makeCard("Actual %", fmtPctRaw(status.actual_allocation_pct)),
        makeCard("Drift", fmtPctRaw(status.drift_pct), null, signClass(status.drift_pct)),
        makeCard("Entry date", fmtDate(status.entry_date_pipeline)),
        makeCard("Entry price", fmtMoney(status.entry_price_pipeline)),
      ]));
    }

    wrap.appendChild(el("h2", { class: "section-title" }, "Live position (Alpaca)"));
    if (!live) {
      wrap.appendChild(renderEmpty("Not currently held", ""));
    } else {
      wrap.appendChild(el("div", { class: "cards" }, [
        makeCard("Qty", fmtNum(live.qty)),
        makeCard("Current price", fmtMoney(live.current_price)),
        makeCard("Market value", fmtMoney(live.market_value)),
        makeCard("Cost basis", fmtMoney(live.cost_basis)),
        makeCard("Unrealised P&L", fmtMoney(live.unrealized_pl), null, signClass(live.unrealized_pl)),
        makeCard("Unrealised %", fmtPct(live.unrealized_plpc), null, signClass(live.unrealized_plpc)),
      ]));
    }
    return wrap;
  }

  function renderTickerHistoryTab(history) {
    if (!history || !history.length) return renderEmpty("No history", "This ticker hasn't appeared in any past reconciliation.");
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Entry"),
      el("th", { class: "num" }, "Entry $"),
      el("th", {}, "Exit"),
      el("th", { class: "num" }, "Days held"),
      el("th", {}, "Sector"),
      el("th", { class: "num" }, "Alloc %"),
    ])));
    const tb = el("tbody");
    history.forEach((h) => {
      tb.appendChild(el("tr", {}, [
        el("td", {}, fmtDate(h.entry_date)),
        el("td", { class: "num" }, fmtMoney(h.entry_price)),
        el("td", {}, fmtDate(h.exit_date) || "held"),
        el("td", { class: "num" }, fmtInt(h.holding_period_days)),
        el("td", {}, h.sector || "—"),
        el("td", { class: "num" }, fmtPctRaw(h.allocation_pct_at_entry)),
      ]));
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  // ============ Forecasts ============

  async function renderForecasts() {
    app.replaceChildren(el("div", { class: "loading" }, "Loading forecasts…"));
    const acc = await fetchJson("prediction_accuracy.json");
    if (!acc) {
      app.replaceChildren(renderEmpty("No prediction accuracy yet", "Run Stage 6 to generate forecasts."));
      return;
    }
    const wrap = el("div");
    wrap.appendChild(el("h1", { class: "view-title" }, "Forecast accuracy"));

    const agg = acc.aggregate || {};
    wrap.appendChild(el("div", { class: "cards" }, [
      makeCard("Evaluated", fmtInt(agg.n_evaluated)),
      makeCard("Pending", fmtInt(agg.n_pending)),
    ]));

    // Mean error by horizon
    const byH = agg.by_horizon || {};
    wrap.appendChild(el("h2", { class: "section-title" }, "Mean error by horizon"));
    const tWrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Horizon"),
      el("th", { class: "num" }, "N"),
      el("th", { class: "num" }, "Mean error"),
      el("th", { class: "num" }, "Mean abs error"),
    ])));
    const tb = el("tbody");
    ["1m", "3m", "6m", "12m"].forEach((h) => {
      const row = byH[h] || {};
      tb.appendChild(el("tr", {}, [
        el("td", {}, h),
        el("td", { class: "num" }, fmtInt(row.n)),
        el("td", { class: "num " + signClass(row.mean_error) }, fmtPct(row.mean_error)),
        el("td", { class: "num" }, fmtPct(row.mean_abs_error)),
      ]));
    });
    t.appendChild(tb);
    tWrap.appendChild(t);
    wrap.appendChild(tWrap);

    // By conviction
    const byC = agg.by_conviction || {};
    if (Object.keys(byC).length) {
      wrap.appendChild(el("h2", { class: "section-title" }, "Mean error by conviction"));
      const w2 = el("div", { class: "table-wrap" });
      const t2 = el("table");
      t2.appendChild(el("thead", {}, el("tr", {}, [
        el("th", {}, "Conviction"),
        el("th", { class: "num" }, "N"),
        el("th", { class: "num" }, "Mean error"),
        el("th", { class: "num" }, "Mean abs error"),
      ])));
      const tb2 = el("tbody");
      Object.entries(byC).forEach(([k, v]) => {
        tb2.appendChild(el("tr", {}, [
          el("td", {}, k),
          el("td", { class: "num" }, fmtInt(v.n)),
          el("td", { class: "num " + signClass(v.mean_error) }, fmtPct(v.mean_error)),
          el("td", { class: "num" }, fmtPct(v.mean_abs_error)),
        ]));
      });
      t2.appendChild(tb2);
      w2.appendChild(t2);
      wrap.appendChild(w2);
    }

    // Evaluated table
    wrap.appendChild(el("h2", { class: "section-title" }, `Evaluated (${(acc.evaluated || []).length})`));
    wrap.appendChild(evaluatedTable(acc.evaluated || []));

    // Pending
    wrap.appendChild(el("h2", { class: "section-title" }, `Pending (${(acc.pending || []).length})`));
    wrap.appendChild(pendingTable(acc.pending || []));

    app.replaceChildren(wrap);
  }

  function evaluatedTable(rows) {
    const wrap = el("div", { class: "table-wrap" });
    if (!rows.length) {
      wrap.appendChild(renderEmpty("Nothing evaluated yet", "Predictions reach their first horizon ~30 days after entry."));
      return wrap;
    }
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Ticker"),
      el("th", {}, "Entry"),
      el("th", {}, "Horizon"),
      el("th", { class: "num" }, "Entry $"),
      el("th", { class: "num" }, "Actual $"),
      el("th", { class: "num" }, "Actual %"),
      el("th", { class: "num" }, "Expected %"),
      el("th", { class: "num" }, "Error"),
      el("th", {}, "Conviction"),
    ])));
    const tb = el("tbody");
    rows.forEach((r) => {
      const tr = el("tr", { class: "row-link" });
      tr.addEventListener("click", () => { window.location.hash = `#/ticker/${encodeURIComponent(r.ticker)}`; });
      tr.append(
        el("td", { class: "ticker" }, r.ticker),
        el("td", {}, fmtDate(r.entry_date)),
        el("td", {}, r.horizon),
        el("td", { class: "num" }, fmtMoney(r.entry_price)),
        el("td", { class: "num" }, fmtMoney(r.actual_price)),
        el("td", { class: "num " + signClass(r.actual_return) }, fmtPct(r.actual_return)),
        el("td", { class: "num " + signClass(r.expected_return) }, fmtPct(r.expected_return)),
        el("td", { class: "num " + signClass(r.error) }, fmtPct(r.error)),
        el("td", {}, r.conviction || "—")
      );
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  function pendingTable(rows) {
    const wrap = el("div", { class: "table-wrap" });
    if (!rows.length) {
      wrap.appendChild(renderEmpty("No pending forecasts", ""));
      return wrap;
    }
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Ticker"),
      el("th", {}, "Entry"),
      el("th", {}, "Horizon"),
      el("th", {}, "Target date"),
      el("th", { class: "num" }, "Expected %"),
      el("th", {}, "Conviction"),
    ])));
    const tb = el("tbody");
    rows.forEach((r) => {
      const tr = el("tr", { class: "row-link" });
      tr.addEventListener("click", () => { window.location.hash = `#/ticker/${encodeURIComponent(r.ticker)}`; });
      tr.append(
        el("td", { class: "ticker" }, r.ticker),
        el("td", {}, fmtDate(r.entry_date)),
        el("td", {}, r.horizon),
        el("td", {}, fmtDate(r.horizon_target_date)),
        el("td", { class: "num " + signClass(r.expected_return) }, fmtPct(r.expected_return)),
        el("td", {}, r.conviction || "—")
      );
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  // ============ History ============

  async function renderHistory() {
    app.replaceChildren(el("div", { class: "loading" }, "Loading history…"));
    const [ledger, runs] = await Promise.all([
      fetchJson("positions_ledger.json"),
      fetchJson("runs_index.json"),
    ]);
    const wrap = el("div");
    wrap.appendChild(el("h1", { class: "view-title" }, "History"));

    wrap.appendChild(el("h2", { class: "section-title" }, "Positions ledger"));
    if (!ledger || !(ledger.entries || []).length) {
      wrap.appendChild(renderEmpty("No ledger entries", "Stage 4 portfolio history is empty."));
    } else {
      wrap.appendChild(ledgerTable(ledger.entries));
    }

    wrap.appendChild(el("h2", { class: "section-title" }, "Pipeline event timeline"));
    if (!runs || !(runs.events || []).length) {
      wrap.appendChild(renderEmpty("No events", "No pipeline events found in the latest snapshot."));
    } else {
      wrap.appendChild(runsTimeline(runs.events));
    }
    app.replaceChildren(wrap);
  }

  function ledgerTable(entries) {
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Ticker"),
      el("th", {}, "Entry"),
      el("th", {}, "Exit"),
      el("th", { class: "num" }, "Entry $"),
      el("th", { class: "num" }, "Alloc %"),
      el("th", {}, "Sector"),
      el("th", { class: "num" }, "Days held"),
    ])));
    const tb = el("tbody");
    [...entries].reverse().forEach((e) => {
      const tr = el("tr", { class: "row-link" });
      tr.addEventListener("click", () => { window.location.hash = `#/ticker/${encodeURIComponent(e.ticker)}`; });
      tr.append(
        el("td", { class: "ticker" }, e.ticker),
        el("td", {}, fmtDate(e.entry_date)),
        el("td", {}, e.exit_date ? fmtDate(e.exit_date) : "held"),
        el("td", { class: "num" }, fmtMoney(e.entry_price)),
        el("td", { class: "num" }, fmtPctRaw(e.allocation_pct_at_entry)),
        el("td", {}, e.sector || "—"),
        el("td", { class: "num" }, fmtInt(e.holding_period_days))
      );
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  function runsTimeline(events) {
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Date"),
      el("th", {}, "Event"),
      el("th", {}, "Status"),
      el("th", {}, "Detail"),
    ])));
    const tb = el("tbody");
    [...events].reverse().forEach((e) => {
      let detail = "";
      if (e.type === "stage4_execution") {
        detail = `${e.orders_submitted ?? "—"} orders${e.dry_run ? " (dry run)" : ""}`;
      } else if (e.type === "stage4_consolidation") {
        detail = `${e.positions_count ?? "—"} positions · ${e.source || ""}`;
      } else if (e.type === "stage4_reconciliation") {
        detail = e.source || "";
      } else if (e.type === "stage1_universe") {
        detail = `${e.company_count ?? "—"} companies · ${e.source || ""}`;
      } else if (e.type === "stage5_monitor_run") {
        detail = `rerun: ${(e.tickers_to_rerun ?? []).join(", ") || "—"}`;
      }
      tb.appendChild(el("tr", {}, [
        el("td", {}, fmtDate(e.date)),
        el("td", {}, e.type.replace(/_/g, " ")),
        el("td", {}, el("span", { class: "tag muted" }, e.status || "—")),
        el("td", {}, detail),
      ]));
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  // ============ Snapshots ============

  async function renderSnapshots() {
    app.replaceChildren(el("div", { class: "loading" }, "Loading snapshots…"));
    const idx = await fetchJson("snapshots_index.json");
    if (!idx || !(idx.snapshots || []).length) {
      app.replaceChildren(renderEmpty("No snapshots", "Stage 6 hasn't produced any snapshots yet."));
      return;
    }
    const wrap = el("div");
    wrap.appendChild(el("h1", { class: "view-title" }, `Snapshots (${idx.snapshot_count ?? idx.snapshots.length})`));
    const tw = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(el("thead", {}, el("tr", {}, [
      el("th", {}, "Snapshot id"),
      el("th", {}, "Created"),
      el("th", { class: "num" }, "Files"),
      el("th", { class: "num" }, "Δ added"),
      el("th", { class: "num" }, "Δ modified"),
      el("th", { class: "num" }, "Δ removed"),
      el("th", {}, "Consolidation date"),
      el("th", {}, "Reconciled date"),
    ])));
    const tb = el("tbody");
    idx.snapshots.forEach((s) => {
      const ch = s.changes_summary || {};
      const a = s.upstream_anchors || {};
      tb.appendChild(el("tr", {}, [
        el("td", { class: "ticker" }, s.snapshot_id),
        el("td", {}, fmtTs(s.created_at)),
        el("td", { class: "num" }, fmtInt(s.tracked_file_count)),
        el("td", { class: "num" }, fmtInt(ch.added)),
        el("td", { class: "num" }, fmtInt(ch.modified)),
        el("td", { class: "num" }, fmtInt(ch.removed)),
        el("td", {}, fmtDate(a.stage4_consolidation_date)),
        el("td", {}, fmtDate(a.stage4_reconciled_date)),
      ]));
    });
    t.appendChild(tb);
    tw.appendChild(t);
    wrap.appendChild(tw);
    app.replaceChildren(wrap);
  }

  // ============ Boot ============

  window.addEventListener("hashchange", route);
  window.addEventListener("DOMContentLoaded", route);
})();
