(function () {
  "use strict";
  var root = document.documentElement;
  var button = document.getElementById("themeToggle");
  function update(theme) {
    root.dataset.theme = theme;
    if (!button) return;
    var dark = theme === "dark";
    button.setAttribute("aria-pressed", String(dark));
    var label = button.querySelector(".theme-toggle-label");
    if (label) label.textContent = dark ? "Light" : "Dark";
  }
  update(root.dataset.theme || "light");
  if (button) button.addEventListener("click", function () {
    var theme = root.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem("bazaar-theme", theme); } catch (e) {}
    update(theme);
  });
})();
