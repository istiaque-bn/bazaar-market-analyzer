(function () {
  const trackDse = document.getElementById("marketTickerTrackDse");
  const trackCse = document.getElementById("marketTickerTrackCse");
  const badgeDse = document.getElementById("tickerLiveBadgeDse");
  const badgeCse = document.getElementById("tickerLiveBadgeCse");
  const statusBox = document.getElementById("marketStatus");
  // trackCse/badgeCse are absent whenever CSE is disabled for this
  // deployment (base.html omits that markup entirely server-side) — only
  // DSE's track is required for the ticker to be worth running at all.
  if (!trackDse) return;

  function formatPct(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
    const n = Number(v);
    const cls = n >= 0 ? "up" : "down";
    const sign = n >= 0 ? "+" : "";
    return `<span class="${cls}">${sign}${n.toFixed(2)}%</span>`;
  }

  function updateBadge(el, exchange, sync, openHint) {
    if (!el) return;
    el.classList.remove("is-live", "is-demo", "is-syncing", "is-stale");
    if (sync && sync.running) {
      el.textContent = "SYNC";
      el.classList.add("is-syncing");
      el.title = `${exchange} · fetching live quotes…`;
      return;
    }
    if (sync && sync.last_success) {
      const ageMin = (Date.now() - new Date(sync.last_success).getTime()) / 60000;
      if (ageMin < 5) {
        el.textContent = sync.market_open ? "LIVE" : "AUTO";
        el.classList.add("is-live");
      } else {
        el.textContent = "AUTO";
        el.classList.add("is-stale");
      }
      el.title = `${exchange} · last sync ${Math.round(ageMin * 10) / 10}m ago` +
        (sync.source ? ` via ${sync.source}` : "") +
        (openHint ? ` · ${openHint}` : "");
      return;
    }
    el.textContent = sync && sync.last_error ? "ERR" : "WAIT";
    el.classList.add("is-demo");
    el.title = `${exchange} · ` + (sync && sync.last_error ? sync.last_error : "Waiting for first sync…");
  }

  function boardHtml(board, exchange) {
    const parts = [];
    const idx = board && board.index;
    if (idx) {
      parts.push(
        `<span class="ticker-item ticker-index"><strong>${exchange}</strong>` +
          (idx.index_value != null ? `<span>${Number(idx.index_value).toFixed(2)}</span>` : "") +
          formatPct(idx.index_change_pct) +
          `</span>`
      );
    }
    (board && board.quotes ? board.quotes : []).forEach((q) => {
      const href = `/stocks/${q.exchange}/${q.trading_code}/`;
      parts.push(
        `<a class="ticker-item" href="${href}">` +
          `<strong>${q.trading_code}</strong>` +
          `<span>${Number(q.last_price).toFixed(2)}</span>` +
          formatPct(q.last_change_pct) +
          `</a>`
      );
    });
    if (!parts.length) {
      parts.push(`<span class="ticker-item">${exchange} quotes loading…</span>`);
    }
    return parts.join("") + parts.join("");
  }

  function fillTrack(track, html) {
    if (!track) return;
    track.innerHTML = html;
    const n = Math.max(1, track.children.length / 2);
    track.style.setProperty("--ticker-duration", `${Math.max(40, n * 2.2)}s`);
    track.dataset.duplicated = "1";
  }

  function updateMarketStatus(hours) {
    if (!statusBox || !hours) return;
    ["dse", "cse"].forEach((key) => {
      const info = hours[key];
      if (!info) return;
      const chip = statusBox.querySelector(`[data-ex="${info.exchange}"]`);
      if (!chip) return;
      chip.classList.toggle("is-open", !!info.is_open);
      chip.classList.toggle("is-closed", !info.is_open);
      const state = chip.querySelector(".ms-state");
      const next = chip.querySelector(".ms-next");
      if (state) state.textContent = info.status;
      if (next) {
        next.textContent = `${info.is_open ? "closes" : "opens"} ${info.next_when}`;
      }
    });
  }

  function render(payload) {
    const dse = payload.dse || { quotes: (payload.quotes || []).filter((q) => q.exchange === "DSE") };
    const cse = payload.cse || { quotes: (payload.quotes || []).filter((q) => q.exchange === "CSE") };
    fillTrack(trackDse, boardHtml(dse, "DSE"));
    fillTrack(trackCse, boardHtml(cse, "CSE"));
    updateBadge(badgeDse, "DSE", payload.sync, payload.market_hours && payload.market_hours.dse && payload.market_hours.dse.message);
    updateBadge(badgeCse, "CSE", payload.sync, payload.market_hours && payload.market_hours.cse && payload.market_hours.cse.message);
    updateMarketStatus(payload.market_hours);
  }

  function duplicateInitial(track) {
    if (!track) return;
    if (track.children.length > 0 && !track.dataset.duplicated) {
      track.innerHTML = track.innerHTML + track.innerHTML;
      track.dataset.duplicated = "1";
      const n = track.children.length / 2;
      track.style.setProperty("--ticker-duration", `${Math.max(40, n * 2.2)}s`);
    }
  }
  duplicateInitial(trackDse);
  duplicateInitial(trackCse);

  function tickLocalClock() {
    const el = document.getElementById("localClockTime");
    if (!el) return;
    try {
      const fmt = new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Dhaka",
        weekday: "short",
        day: "numeric",
        month: "short",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      });
      el.textContent = fmt.format(new Date()).replace(",", " ·");
    } catch (_) {
      /* keep server-rendered time */
    }
  }
  tickLocalClock();
  setInterval(tickLocalClock, 30000);

  async function refresh(force) {
    try {
      const url = force ? "/ticker.json?refresh=1" : "/ticker.json";
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      render(await res.json());
    } catch (_) {
      /* keep existing tape */
    }
  }

  function pollMs() {
    const now = new Date();
    const dhakaHour = (now.getUTCHours() + 6) % 24;
    const day = now.getUTCDay();
    const openDay = day === 0 || day === 1 || day === 2 || day === 3 || day === 4;
    const openHour = dhakaHour >= 10 && dhakaHour < 15;
    return openDay && openHour ? 20000 : 60000;
  }

  refresh(true);
  let timer = setInterval(() => {
    if (!document.hidden) refresh(false);
  }, pollMs());
  setInterval(() => {
    clearInterval(timer);
    timer = setInterval(() => {
      if (!document.hidden) refresh(false);
    }, pollMs());
  }, 5 * 60 * 1000);
})();
