(function () {
  const table = document.getElementById("holdingsTable");
  const refreshBtn = document.getElementById("portfolioRefreshBtn");
  const lastUpdatedEl = document.getElementById("portfolioLastUpdatedTime");
  const quotesUrl = window.PORTFOLIO_QUOTES_URL;

  // --- BDT formatting (mirrors market/templatetags/portfolio_extras.py) ---
  function bdt(value, decimals) {
    decimals = decimals === undefined ? 2 : decimals;
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    if (Number.isNaN(n)) return "—";
    const sign = n < 0 ? "-" : "";
    const abs = Math.abs(n).toFixed(decimals);
    const [intPart, fracPart] = abs.split(".");
    let grouped = intPart;
    if (intPart.length > 3) {
      const last3 = intPart.slice(-3);
      let rest = intPart.slice(0, -3);
      const groups = [];
      while (rest.length > 2) {
        groups.unshift(rest.slice(-2));
        rest = rest.slice(0, -2);
      }
      if (rest) groups.unshift(rest);
      grouped = groups.join(",") + "," + last3;
    }
    return `৳${sign}${grouped}${fracPart !== undefined && decimals ? "." + fracPart : ""}`;
  }

  function signed(value, decimals) {
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    if (Number.isNaN(n)) return "—";
    const formatted = n.toFixed(decimals === undefined ? 2 : decimals);
    return n > 0 ? `+${formatted}` : formatted;
  }

  function plClass(value) {
    if (value === null || value === undefined || value === "") return "";
    const n = Number(value);
    if (Number.isNaN(n) || n === 0) return "";
    return n > 0 ? "up" : "down";
  }

  // --- Live price polling ---------------------------------------------

  function updateRow(row, quote) {
    const setField = (field, text, cls) => {
      const cell = row.querySelector(`[data-field="${field}"]`);
      if (!cell) return;
      cell.textContent = text;
      if (cls !== undefined) {
        cell.classList.remove("up", "down");
        if (cls) cell.classList.add(cls);
      }
    };
    setField("latest_price", quote.latest_price !== null ? bdt(quote.latest_price) : "—");
    setField("market_value", bdt(quote.market_value));
    const unrealizedCell = row.querySelector('[data-field="unrealized_pl"]');
    if (unrealizedCell) {
      unrealizedCell.classList.remove("up", "down");
      const cls = plClass(quote.unrealized_pl);
      if (cls) unrealizedCell.classList.add(cls);
      const pctText = quote.unrealized_pl_pct !== null ? `<br><span class="tiny">(${signed(quote.unrealized_pl_pct)}%)</span>` : "";
      unrealizedCell.innerHTML = `${signed(quote.unrealized_pl)}${pctText}`;
    }
    setField("today_pl", quote.today_pl !== null ? signed(quote.today_pl) : "—", plClass(quote.today_pl));
    setField("allocation_pct", quote.allocation_pct !== null ? `${quote.allocation_pct}%` : "—");

    const badge = row.querySelector(".quote-badge");
    if (badge) {
      badge.className = `badge quote-badge quote-${quote.quote_status}`;
      badge.textContent = quote.quote_label;
      if (quote.quote_as_of) {
        badge.title = `as of ${new Date(quote.quote_as_of).toLocaleString("en-GB", { timeZone: "Asia/Dhaka" })}`;
      }
    }
    row.dataset.plSign = plClass(quote.unrealized_pl);
  }

  function updateSummary(payload) {
    const setStat = (id, text, cls) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove("up", "down");
      if (cls) el.classList.add(cls);
      el.textContent = text;
    };
    const marketValueEl = document.getElementById("statMarketValue");
    if (marketValueEl) marketValueEl.textContent = bdt(payload.total_market_value);
    const unrealizedEl = document.getElementById("statUnrealizedPl");
    if (unrealizedEl) {
      unrealizedEl.classList.remove("up", "down");
      const cls = plClass(payload.total_unrealized_pl);
      if (cls) unrealizedEl.classList.add(cls);
      const pctText = payload.total_unrealized_pl_pct !== null ? ` <span class="tiny">(${signed(payload.total_unrealized_pl_pct)}%)</span>` : "";
      unrealizedEl.innerHTML = `${signed(payload.total_unrealized_pl)}${pctText}`;
    }
    setStat("statTodayPl", payload.today_total_pl !== null ? signed(payload.today_total_pl) : "—", plClass(payload.today_total_pl));
  }

  async function refresh() {
    if (!quotesUrl) return;
    if (refreshBtn) refreshBtn.disabled = true;
    try {
      const res = await fetch(quotesUrl, { headers: { Accept: "application/json" } });
      if (res.status === 429) return;
      if (!res.ok) return;
      const payload = await res.json();
      if (table) {
        (payload.holdings || []).forEach((quote) => {
          const row = table.querySelector(`tr[data-quote-row="${quote.exchange}:${quote.trading_code}"]`);
          if (row) updateRow(row, quote);
        });
      }
      updateSummary(payload);
      if (lastUpdatedEl) {
        lastUpdatedEl.textContent = new Date(payload.generated_at).toLocaleTimeString("en-GB", {
          timeZone: "Asia/Dhaka",
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
        });
      }
    } catch (_) {
      /* keep last-known values on a transient failure */
    } finally {
      if (refreshBtn) refreshBtn.disabled = false;
    }
  }

  function pollMs() {
    const now = new Date();
    const dhakaHour = (now.getUTCHours() + 6) % 24;
    const day = now.getUTCDay();
    const openDay = day >= 0 && day <= 4;
    const openHour = dhakaHour >= 10 && dhakaHour < 15;
    return openDay && openHour ? 20000 : 60000;
  }

  if (quotesUrl) {
    refresh();
    let timer = setInterval(() => {
      if (!document.hidden) refresh();
    }, pollMs());
    // Re-evaluate the interval periodically in case market hours changed
    // mid-session (matches static/js/ticker.js's own approach).
    setInterval(() => {
      clearInterval(timer);
      timer = setInterval(() => {
        if (!document.hidden) refresh();
      }, pollMs());
    }, 5 * 60 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });
  }
  if (refreshBtn) refreshBtn.addEventListener("click", refresh);

  // --- Client-side sort/filter (holdings table is per-portfolio-sized,
  // not paginated, so this is a plain in-browser sort — no server round
  // trip needed) --------------------------------------------------------

  if (table) {
    const tbody = table.querySelector("tbody");
    let sortState = { key: null, dir: 1 };

    function cellSortValue(row, key) {
      if (key === "trading_code") return row.dataset.code || "";
      const cell = row.querySelector(`[data-field="${key}"]`);
      if (!cell) return "";
      const text = (cell.textContent || "").replace(/[৳,+%]/g, "").replace(/\(.*\)/, "").trim();
      const n = parseFloat(text);
      return Number.isNaN(n) ? text.toLowerCase() : n;
    }

    table.querySelectorAll("th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        sortState.dir = sortState.key === key ? -sortState.dir : 1;
        sortState.key = key;
        table.querySelectorAll("th.sortable").forEach((h) => h.classList.remove("sort-asc", "sort-desc"));
        th.classList.add(sortState.dir === 1 ? "sort-asc" : "sort-desc");

        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.sort((a, b) => {
          const av = cellSortValue(a, key);
          const bv = cellSortValue(b, key);
          if (av < bv) return -1 * sortState.dir;
          if (av > bv) return 1 * sortState.dir;
          return 0;
        });
        rows.forEach((r) => tbody.appendChild(r));
      });
    });

    const exchangeFilter = document.getElementById("holdingsExchangeFilter");
    const plFilter = document.getElementById("holdingsPlFilter");
    const searchInput = document.getElementById("holdingsSearch");

    function applyFilters() {
      const exchange = exchangeFilter ? exchangeFilter.value : "";
      const pl = plFilter ? plFilter.value : "";
      const search = searchInput ? searchInput.value.trim().toLowerCase() : "";
      tbody.querySelectorAll("tr").forEach((row) => {
        let visible = true;
        if (exchange && row.dataset.exchange !== exchange) visible = false;
        if (pl && row.dataset.plSign !== pl) visible = false;
        if (search && !(row.dataset.code || "").toLowerCase().includes(search) && !(row.dataset.company || "").includes(search)) {
          visible = false;
        }
        row.style.display = visible ? "" : "none";
      });
    }
    if (exchangeFilter) exchangeFilter.addEventListener("change", applyFilters);
    if (plFilter) plFilter.addEventListener("change", applyFilters);
    if (searchInput) searchInput.addEventListener("input", applyFilters);
  }
})();
