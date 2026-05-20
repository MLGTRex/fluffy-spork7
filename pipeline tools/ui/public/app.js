/*
 * Public Stage 6 dashboard.
 *
 * Reads exactly one file: ./data/public_summary.json (override base via
 * `?dataRoot=…` for local dev). Renders performance cards, returns table,
 * and a value-vs-SPY normalised chart. Never fetches dossiers or per-ticker
 * data — that lives at /admin/data/ behind a JS password gate.
 *
 * Empty/stale-state: if the file is missing or marks live data stale, a
 * yellow banner explains and the page still renders with whatever data is
 * available.
 */

(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const DATA_ROOT = (params.get("dataRoot") || "./data").replace(/\/+$/, "");

  // ============ Helpers ============

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

  const fmtPct = (v, dp = 2) =>
    v === null || v === undefined || isNaN(v) ? "—" : `${(v * 100).toFixed(dp)}%`;
  const fmtNum = (v, dp = 2) =>
    v === null || v === undefined || isNaN(v)
      ? "—"
      : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const fmtInt = (v) =>
    v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US");
  const fmtTs = (s) => {
    if (!s) return "—";
    try {
      return new Date(s).toISOString().replace("T", " ").slice(0, 19) + "Z";
    } catch {
      return String(s);
    }
  };
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

  // ============ Render ============

  function renderEmpty(targetEl, msg) {
    targetEl.replaceChildren(el("div", { class: "empty" }, msg));
  }

  function renderStaleBanner(summary) {
    const banner = document.getElementById("perf-stale");
    if (summary && summary.live_stale) {
      banner.textContent =
        "Live performance refreshes hourly; values shown are from the most recent successful refresh.";
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }

  function makeCard(label, value, sub, valueClass) {
    return el("div", { class: "card" }, [
      el("div", { class: "label" }, label),
      el("div", { class: "value " + (valueClass || "") }, String(value)),
      sub !== undefined ? el("div", { class: "sub" }, String(sub)) : null,
    ]);
  }

  function renderCards(perf) {
    const container = document.getElementById("perf-cards");
    container.replaceChildren(
      makeCard(
        "Total return",
        fmtPct(perf.total_return_pct),
        perf.since_inception_days != null ? `${perf.since_inception_days} days live` : null,
        signClass(perf.total_return_pct)
      ),
      makeCard(
        "vs S&P 500",
        fmtPct(
          perf.total_return_pct != null && perf.spy_total_return_pct != null
            ? perf.total_return_pct - perf.spy_total_return_pct
            : null
        ),
        `SPY: ${fmtPct(perf.spy_total_return_pct)}`,
        signClass(
          perf.total_return_pct != null && perf.spy_total_return_pct != null
            ? perf.total_return_pct - perf.spy_total_return_pct
            : null
        )
      ),
      makeCard(
        "Max drawdown",
        fmtPct(perf.max_drawdown_pct),
        `now: ${fmtPct(perf.current_drawdown_pct)}`,
        signClass(perf.max_drawdown_pct)
      ),
      makeCard(
        "30-day Sharpe",
        fmtNum(perf.rolling_30d_sharpe, 2),
        "annualised, rolling"
      )
    );
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
      const excess =
        p != null && spy != null ? p - spy : null;
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
    document.getElementById("returns-table").replaceChildren(t);
  }

  function renderChart(valueSeries, benchSeries, benchSymbol) {
    if (typeof Chart === "undefined") return;
    const canvas = document.getElementById("value-chart");
    const wrap = canvas.parentNode;

    if ((!valueSeries || !valueSeries.length) && (!benchSeries || !benchSeries.length)) {
      renderEmpty(wrap, "No performance history available yet.");
      return;
    }
    const v = valueSeries || [];
    const b = benchSeries || [];
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
            label: benchSymbol || "SPY",
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
          y: {
            ticks: {
              callback: (v) => (v * 100 - 100).toFixed(1) + "%",
            },
          },
        },
      },
    });
  }

  function renderFooter(summary) {
    const parts = [];
    if (summary.as_of) parts.push(`as of ${fmtTs(summary.as_of)}`);
    if (summary.snapshot_id) parts.push(`snapshot ${summary.snapshot_id}`);
    document.getElementById("footer-meta").textContent = parts.join("  ·  ");
  }

  // ============ Boot ============

  async function boot() {
    const summary = await fetchJson("public_summary.json");
    if (!summary) {
      const cardsEl = document.getElementById("perf-cards");
      renderEmpty(
        cardsEl,
        "Performance data not yet available — the pipeline hasn't published a public summary."
      );
      const chartWrap = document.querySelector(".chart-wrap");
      if (chartWrap) renderEmpty(chartWrap, "");
      const returnsEl = document.getElementById("returns-table");
      if (returnsEl) renderEmpty(returnsEl, "");
      return;
    }
    renderStaleBanner(summary);
    renderCards(summary.performance || {});
    renderReturnsTable(summary.performance || {});
    renderChart(
      summary.value_history || [],
      summary.benchmark_history || [],
      ((summary.benchmark_history || [])[0] || {}).symbol || "SPY"
    );
    renderFooter(summary);
  }

  window.addEventListener("DOMContentLoaded", boot);
})();
