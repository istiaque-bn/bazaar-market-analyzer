(function () {
  "use strict";

  var toggle = document.getElementById("navToggle");
  var nav = document.getElementById("siteNav");
  var scrim = document.getElementById("navScrim");
  var closeBtn = document.getElementById("navClose");
  if (!nav || !toggle) return;

  var mql = window.matchMedia("(max-width: 900px)");
  var lastFocused = null;
  var scrimHideTimer = null;

  function focusableEls() {
    return Array.prototype.slice
      .call(nav.querySelectorAll('a[href], button:not([disabled]), input, [tabindex]:not([tabindex="-1"])'))
      .filter(function (el) {
        return el.offsetParent !== null;
      });
  }

  function isDrawerOpen() {
    return nav.classList.contains("is-open");
  }

  function openDrawer() {
    lastFocused = document.activeElement;
    nav.classList.add("is-open");
    toggle.setAttribute("aria-expanded", "true");
    document.body.classList.add("nav-open");
    if (scrim) {
      window.clearTimeout(scrimHideTimer);
      scrim.hidden = false;
      requestAnimationFrame(function () {
        scrim.classList.add("is-visible");
      });
    }
    var focusables = focusableEls();
    if (focusables.length) focusables[0].focus();
  }

  function closeDrawer(restoreFocus) {
    nav.classList.remove("is-open");
    toggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("nav-open");
    if (scrim) {
      scrim.classList.remove("is-visible");
      scrimHideTimer = window.setTimeout(function () {
        scrim.hidden = true;
      }, 200);
    }
    closeAllDropdowns();
    if (restoreFocus !== false && lastFocused && typeof lastFocused.focus === "function") {
      lastFocused.focus();
    }
  }

  toggle.addEventListener("click", function () {
    if (isDrawerOpen()) closeDrawer();
    else openDrawer();
  });
  if (closeBtn) closeBtn.addEventListener("click", function () { closeDrawer(); });
  if (scrim) scrim.addEventListener("click", function () { closeDrawer(); });

  nav.addEventListener("click", function (e) {
    if (!mql.matches) return;
    var target = e.target.closest("a[href], .nav-logout-btn");
    if (target) closeDrawer(false);
  });

  window.addEventListener("resize", function () {
    if (!mql.matches && isDrawerOpen()) closeDrawer(false);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Tab" && isDrawerOpen()) {
      var focusables = focusableEls();
      if (!focusables.length) return;
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  var dropdowns = Array.prototype.slice.call(nav.querySelectorAll(".nav-dropdown"));

  function closeDropdown(dd, focusButton) {
    var btn = dd.querySelector(".nav-dropdown-btn");
    dd.classList.remove("is-open");
    if (btn) {
      btn.setAttribute("aria-expanded", "false");
      if (focusButton) btn.focus();
    }
  }

  function closeAllDropdowns(except) {
    dropdowns.forEach(function (dd) {
      if (dd !== except) closeDropdown(dd);
    });
  }

  function openDropdown(dd) {
    closeAllDropdowns(dd);
    var btn = dd.querySelector(".nav-dropdown-btn");
    dd.classList.add("is-open");
    if (btn) btn.setAttribute("aria-expanded", "true");
  }

  dropdowns.forEach(function (dd) {
    var btn = dd.querySelector(".nav-dropdown-btn");
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (dd.classList.contains("is-open")) closeDropdown(dd);
      else openDropdown(dd);
    });
  });

  document.addEventListener("click", function (e) {
    dropdowns.forEach(function (dd) {
      if (dd.classList.contains("is-open") && !dd.contains(e.target)) closeDropdown(dd);
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (isDrawerOpen()) {
      closeDrawer();
      return;
    }
    var openDd = nav.querySelector(".nav-dropdown.is-open");
    if (openDd) closeDropdown(openDd, true);
  });
})();
