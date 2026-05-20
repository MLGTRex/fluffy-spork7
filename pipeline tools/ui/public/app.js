/*
 * Public Stage 6 dashboard.
 *
 * Routes:
 *   #/              — overview (how-it-works + performance + positions list)
 *   #/ticker/{T}    — per-ticker page (performance, thesis, candidate summary,
 *                     invalidation triggers)
 *
 * Reads exactly one file: ./data/public_summary.json (override base via
 * `?dataRoot=…` for local dev). Fail-soft on missing data — never throws.
 */

(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const DATA_ROOT = (params.get("dataRoot") || "./data").replace(/\/+$/, "");

  const app = document.getElementById("app");

  // Cached so we only fetch once per page-load; route() reuses across views.
  let SUMMARY_PROMISE = null;

  // ============ Data ============

  async function fetchJson(rel) {
    try {
      const res = await fetch(`${DATA_ROOT}/${rel}`, { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      console.warn(`fetchJson ${rel} failed`, e);
      return null;
    }
  }

  function getSummary() {
    if (!SUMMARY_PROMISE) SUMMARY_PROMISE = fetchJson("public_summary.json");
    return SUMMARY_PROMISE;
  }

  // ============ Formatters ============

  const fmtPct = (v, dp = 2) =>
    v === null || v === undefined || isNaN(v) ? "—" : `${(v * 100).toFixed(dp)}%`;
  const fmtPctRaw = (v, dp = 2) =>
    v === null || v === undefined || isNaN(v) ? "—" : `${Number(v).toFixed(dp)}%`;
  const fmtMoney = (v, dp = 2) =>
    v === null || v === undefined || isNaN(v)
      ? "—"
      : Number(v).toLocaleString("en-US", {
          style: "currency",
          currency: "USD",
          minimumFractionDigits: dp,
          maximumFractionDigits: dp,
        });
  const fmtNum = (v, dp = 2) =>
    v === null || v === undefined || isNaN(v)
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const fmtTs = (s) => {
    if (!s) return "—";
    try {
      return new Date(s).toISOString().replace("T", " ").slice(0, 19) + "Z";
    } catch {
      return String(s);
    }
  };
  const fmtDate = (s) => (s ? String(s).slice(0, 10) : "—");
  const signClass = (v) => (v === null || v === undefined ? "" : v >= 0 ? "pos" : "neg");

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else node.setAttribute(k, v);
    });
    (Array.isArray(children) ? children : children !== undefined ? [children] : []).forEach((c) => {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function renderEmpty(msg) {
    return el("div", { class: "empty" }, msg);
  }

  function makeCard(label, value, sub, valueClass) {
    return el("div", { class: "card" }, [
      el("div", { class: "label" }, label),
      el("div", { class: "value " + (valueClass || "") }, String(value)),
      sub !== undefined && sub !== null ? el("div", { class: "sub" }, String(sub)) : null,
    ]);
  }

  function setFooter(summary) {
    const parts = [];
    if (summary && summary.as_of) parts.push(`as of ${fmtTs(summary.as_of)}`);
    if (summary && summary.snapshot_id) parts.push(`snapshot ${summary.snapshot_id}`);
    document.getElementById("footer-meta").textContent = parts.join("  ·  ");
  }

  function staleBanner(summary) {
    if (!summary || !summary.live_stale) return null;
    return el(
      "div",
      { class: "stale-card" },
      "Live data is being cached — values shown are from the most recent successful refresh."
    );
  }

  // ============ Router ============

  async function route() {
    const hash = window.location.hash || "#/";
    const parts = hash.replace(/^#\//, "").split("/").filter(Boolean);
    if (parts[0] === "ticker" && parts[1]) {
      await renderTickerView(decodeURIComponent(parts[1]));
    } else {
      await renderOverview();
    }
    window.scrollTo(0, 0);
  }

  // ============ Overview view ============

  async function renderOverview() {
    app.replaceChildren(el("div", { class: "loading" }, "Loading overview…"));
    const summary = await getSummary();
    setFooter(summary || {});

    const container = el("div");

    if (!summary) {
      container.appendChild(renderHowItWorks());
      container.appendChild(
        el(
          "section",
          { class: "section" },
          renderEmpty(
            "Performance data not yet available — the pipeline hasn't published a public summary."
          )
        )
      );
      app.replaceChildren(container);
      return;
    }

    // 1. How it works (always rendered, even when data is partial)
    container.appendChild(renderHowItWorks());

    // 2. Performance
    container.appendChild(renderPerformanceSection(summary));

    // 3. Positions
    container.appendChild(renderPositionsSection(summary));

    app.replaceChildren(container);

    // Chart needs to be drawn AFTER the canvas is in the DOM
    drawValueChart(summary);
  }

  function renderHowItWorks() {
    const section = el("section", { class: "section" });
    section.appendChild(el("h2", {}, "How it works"));
    section.appendChild(
      el(
        "p",
        { class: "lead" },
        "The pipeline runs six sequential stages, end-to-end, every week. Each stage consumes the previous stage's output and adds a new layer of analysis, culminating in a 15-position portfolio executed against a brokerage account."
      )
    );
    const stages = [
      [
        "Universe & ranking",
        "Approximately one thousand listed companies are scored across financial fundamentals, professional-data signals (analyst sentiment, insider activity, institutional positioning) and news sentiment. The combined composite is screened against recent SEC filings for disqualifying events, producing a shortlist for deeper analysis.",
      ],
      [
        "Multi-agent research & debate",
        "Each shortlisted company is investigated in parallel by three research agents covering financial fundamentals, news flow and competitive landscape. Separate bull-case and bear-case LLM agents then debate the company, issue rebuttals against each other's arguments, and a synthesis agent renders a scored verdict.",
      ],
      [
        "Scenario modelling & valuation",
        "Six scenario agents construct bull, base and bear paths in a three-phase debate. An independent quantitative module computes valuation multiples, peer comparisons and five-year history. A consolidator combines everything into price targets at multiple horizons, scenario probabilities, and a probability-weighted expected return.",
      ],
      [
        "Portfolio construction",
        "Two independent tracks propose portfolios: a quant-only MILP optimiser and an LLM portfolio picker that reads the qualitative work from earlier stages. A selector/allocator loop consolidates the two, and a stateful reconciliation step transitions the incumbent book toward the new target with each name change individually adjudicated. Orders route to a paper brokerage.",
      ],
      [
        "Monitor & scheduler",
        "A continuous monitor watches live signals on held positions. Material moves can trigger a narrow re-evaluation of just the affected names between weekly full rebuilds, with the new analysis reconciled back into the standing portfolio.",
      ],
      [
        "Consolidation & backups",
        "Every run is captured as a point-in-time snapshot of all upstream outputs, joined with the live brokerage state. These snapshots are the source of truth for performance attribution, forecast tracking and this dashboard.",
      ],
    ];
    const grid = el("div", { class: "stages" });
    stages.forEach(([title, body], i) => {
      grid.appendChild(
        el("div", { class: "stage" }, [
          el("div", { class: "stage-num" }, String(i + 1)),
          el("h3", {}, title),
          el("p", {}, body),
        ])
      );
    });
    section.appendChild(grid);
    return section;
  }

  function renderPerformanceSection(summary) {
    const section = el("section", { class: "section" });
    section.appendChild(el("h2", {}, "Performance vs benchmark"));
    section.appendChild(
      el(
        "p",
        { class: "lead" },
        "Live portfolio value tracked against the S&P 500 (SPY). Values are normalised to a common start date — the chart shows relative performance, not dollar amounts."
      )
    );

    const stale = staleBanner(summary);
    if (stale) section.appendChild(stale);

    const perf = summary.performance || {};
    const excess =
      perf.total_return_pct != null && perf.spy_total_return_pct != null
        ? perf.total_return_pct - perf.spy_total_return_pct
        : null;
    section.appendChild(
      el("div", { class: "cards" }, [
        makeCard(
          "Total return",
          fmtPct(perf.total_return_pct),
          perf.since_inception_days != null ? `${perf.since_inception_days} days live` : null,
          signClass(perf.total_return_pct)
        ),
        makeCard("vs S&P 500", fmtPct(excess), `SPY: ${fmtPct(perf.spy_total_return_pct)}`, signClass(excess)),
        makeCard("Max drawdown", fmtPct(perf.max_drawdown_pct), `now: ${fmtPct(perf.current_drawdown_pct)}`, signClass(perf.max_drawdown_pct)),
        makeCard("30-day Sharpe", fmtNum(perf.rolling_30d_sharpe, 2), "annualised, rolling"),
      ])
    );

    // Chart (canvas filled in after DOM mount)
    const chartWrap = el("div", { class: "chart-wrap" }, el("canvas", { id: "value-chart" }));
    section.appendChild(chartWrap);

    // Returns table
    section.appendChild(el("h3", { class: "subhead" }, "Returns at common horizons"));
    section.appendChild(renderReturnsTable(perf));
    return section;
  }

  function renderReturnsTable(perf) {
    const rows = [
      ["1 month", perf.m1_return, perf.spy_m1],
      ["3 months", perf.m3_return, perf.spy_m3],
      ["6 months", perf.m6_return, perf.spy_m6],
      ["12 months", perf.m12_return, perf.spy_m12],
      ["Month to date", perf.mtd_return, perf.spy_mtd],
      ["Year to date", perf.ytd_return, perf.spy_ytd],
    ];
    const wrap = el("div", { class: "table-wrap" });
    const t = el("table");
    t.appendChild(
      el("thead", {}, el("tr", {}, [
        el("th", {}, "Horizon"),
        el("th", { class: "num" }, "Portfolio"),
        el("th", { class: "num" }, "S&P 500"),
        el("th", { class: "num" }, "Excess"),
      ]))
    );
    const tbody = el("tbody");
    rows.forEach(([label, p, spy]) => {
      const excess = p != null && spy != null ? p - spy : null;
      tbody.appendChild(
        el("tr", {}, [
          el("td", {}, label),
          el("td", { class: "num " + signClass(p) }, fmtPct(p)),
          el("td", { class: "num " + signClass(spy) }, fmtPct(spy)),
          el("td", { class: "num " + signClass(excess) }, fmtPct(excess)),
        ])
      );
    });
    t.appendChild(tbody);
    wrap.appendChild(t);
    return wrap;
  }

  function drawValueChart(summary) {
    if (typeof Chart === "undefined") return;
    const canvas = document.getElementById("value-chart");
    if (!canvas) return;
    const v = summary.value_history || [];
    const b = summary.benchmark_history || [];
    if (!v.length && !b.length) {
      const wrap = canvas.parentNode;
      wrap.replaceChildren(renderEmpty("No performance history available yet."));
      return;
    }
    const labels = v.length ? v.map((p) => p.date) : b.map((p) => p.date);
    new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Portfolio",
            data: v.map((p) => p.normalised),
            borderColor: "#1e3a8a",
            backgroundColor: "rgba(30, 58, 138, 0.08)",
            tension: 0.18,
            pointRadius: 0,
            borderWidth: 2.5,
          },
          {
            label: (b[0] && b[0].symbol) || "SPY",
            data: b.map((p) => p.normalised),
            borderColor: "#b7791f",
            tension: 0.18,
            pointRadius: 0,
            borderWidth: 2,
            borderDash: [5, 4],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: { legend: { position: "top" } },
        scales: {
          x: { ticks: { maxTicksLimit: 8 } },
          y: { ticks: { callback: (v) => (v * 100 - 100).toFixed(1) + "%" } },
        },
      },
    });
  }

  // ============ Positions section (on overview) ============

  function renderPositionsSection(summary) {
    const section = el("section", { class: "section" });
    section.appendChild(el("h2", {}, "Positions"));
    section.appendChild(
      el(
        "p",
        { class: "lead" },
        "The current portfolio holds 15 positions. Click any card for the thesis, candidate summary and what would invalidate it."
      )
    );

    const positions = summary.positions || [];
    if (!positions.length) {
      section.appendChild(
        renderEmpty(
          "No positions yet — the pipeline hasn't published a target portfolio in the most recent run."
        )
      );
      return section;
    }

    const grid = el("div", { class: "position-grid" });
    positions.forEach((p) => grid.appendChild(renderPositionCard(p)));
    section.appendChild(grid);
    return section;
  }

  function renderPositionCard(p) {
    const todayChange =
      p.current_price != null && p.lastday_price != null && p.lastday_price !== 0
        ? p.current_price / p.lastday_price - 1
        : null;
    const card = el("a", {
      class: "position-card",
      href: `#/ticker/${encodeURIComponent(p.ticker || "")}`,
    }, [
      el("div", { class: "pc-head" }, [
        el("div", { class: "pc-ticker" }, p.ticker || "—"),
        el("div", { class: "pc-meta" }, [
          el("div", { class: "pc-company" }, p.company_name || ""),
          el("div", { class: "pc-tags" }, [
            p.sector ? el("span", { class: "tag" }, p.sector) : null,
            p.conviction ? el("span", { class: "tag accent" }, p.conviction) : null,
          ]),
        ]),
      ]),
      el("div", { class: "pc-stats" }, [
        statBlock("Allocation", fmtPctRaw(p.target_allocation_pct)),
        statBlock("Price", fmtMoney(p.current_price)),
        statBlock("Today", fmtPct(todayChange), signClass(todayChange)),
        statBlock("Unrealised P&L", fmtPct(p.unrealized_plpc), signClass(p.unrealized_plpc)),
      ]),
    ]);
    return card;
  }

  function statBlock(label, value, valueClass) {
    return el("div", { class: "pc-stat" }, [
      el("div", { class: "pc-stat-label" }, label),
      el("div", { class: "pc-stat-value " + (valueClass || "") }, String(value)),
    ]);
  }

  // ============ Per-ticker view ============

  async function renderTickerView(ticker) {
    app.replaceChildren(el("div", { class: "loading" }, `Loading ${ticker}…`));
    const summary = await getSummary();
    setFooter(summary || {});

    const positions = (summary && summary.positions) || [];
    const p = positions.find((x) => (x.ticker || "").toUpperCase() === ticker.toUpperCase());

    if (!p) {
      const wrap = el("div");
      wrap.appendChild(
        el("div", { class: "ticker-hero" }, [
          el("a", { href: "#/", class: "back-link" }, "← All positions"),
          el("h1", { class: "th-ticker" }, ticker),
        ])
      );
      wrap.appendChild(renderEmpty(`No public data for ${ticker}.`));
      app.replaceChildren(wrap);
      return;
    }

    const wrap = el("div");

    // Hero
    const heroTags = [];
    if (p.sector) heroTags.push(el("span", { class: "tag" }, p.sector));
    if (p.conviction) heroTags.push(el("span", { class: "tag accent" }, p.conviction));
    if (p.status) heroTags.push(el("span", { class: "tag muted" }, p.status));
    wrap.appendChild(
      el("div", { class: "ticker-hero" }, [
        el("a", { href: "#/", class: "back-link" }, "← All positions"),
        el("h1", { class: "th-ticker" }, p.ticker),
        p.company_name ? el("div", { class: "th-company" }, p.company_name) : null,
        heroTags.length ? el("div", { class: "th-tags" }, heroTags) : null,
      ])
    );

    // Stale banner if applicable
    if (summary && summary.live_stale) wrap.appendChild(staleBanner(summary));

    // Performance section
    wrap.appendChild(el("h2", { class: "section-title" }, "Performance"));
    const todayChangePct =
      p.current_price != null && p.lastday_price != null && p.lastday_price !== 0
        ? p.current_price / p.lastday_price - 1
        : null;
    const todayChangeDollars =
      p.current_price != null && p.lastday_price != null
        ? p.current_price - p.lastday_price
        : null;
    wrap.appendChild(
      el("div", { class: "cards" }, [
        makeCard("Current price", fmtMoney(p.current_price)),
        makeCard(
          "Today's change",
          fmtPct(todayChangePct),
          fmtMoney(todayChangeDollars),
          signClass(todayChangePct)
        ),
        makeCard("Allocation", fmtPctRaw(p.target_allocation_pct), `actual: ${fmtPctRaw(p.actual_allocation_pct)}`),
        makeCard("Unrealised P&L", fmtPct(p.unrealized_plpc), fmtMoney(p.unrealized_pl), signClass(p.unrealized_plpc)),
      ])
    );

    // Thesis
    wrap.appendChild(el("h2", { class: "section-title" }, "Thesis"));
    if (p.thesis_summary) {
      wrap.appendChild(el("div", { class: "thesis-panel" }, p.thesis_summary));
    } else {
      wrap.appendChild(renderEmpty("No thesis summary available."));
    }

    // Candidate summary
    wrap.appendChild(el("h2", { class: "section-title" }, "Candidate summary"));
    const cs = p.candidate_summary;
    if (cs && cs.summary) {
      const metaBits = [];
      if (cs.source_date) metaBits.push(`source ${fmtDate(cs.source_date)}`);
      if (cs.analysis_date) metaBits.push(`analysis ${fmtDate(cs.analysis_date)}`);
      if (cs.model) metaBits.push(cs.model);
      wrap.appendChild(
        el("div", { class: "thesis-panel" }, [
          metaBits.length ? el("div", { class: "thesis-meta" }, metaBits.join("  ·  ")) : null,
          el("div", { class: "thesis-body" }, cs.summary),
        ])
      );
    } else {
      wrap.appendChild(renderEmpty("No candidate summary available."));
    }

    // Invalidation triggers
    wrap.appendChild(el("h2", { class: "section-title" }, "What would invalidate this thesis"));
    const triggers = p.key_invalidation_triggers || [];
    if (triggers.length) {
      const ul = el("ul", { class: "triggers" });
      triggers.forEach((t) => ul.appendChild(el("li", {}, t)));
      wrap.appendChild(ul);
    } else {
      wrap.appendChild(renderEmpty("No invalidation triggers recorded."));
    }

    app.replaceChildren(wrap);
  }

  // ============ Boot ============

  window.addEventListener("hashchange", route);
  window.addEventListener("DOMContentLoaded", route);
})();
