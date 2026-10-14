// 昼夜主题切换: localStorage 持久化, 服务端 session 做默认值兜底
(function () {
  var KEY = "sa_theme";
  function apply(t) {
    document.documentElement.setAttribute("data-theme", t);
  }
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* 隐私模式 */ }
  apply(saved || document.documentElement.getAttribute("data-theme") || "light");

  var btn = document.getElementById("themeToggle");
  if (btn) {
    btn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      var next = cur === "dark" ? "light" : "dark";
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* 忽略 */ }
      fetch("/api/theme", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": (document.querySelector('meta[name="csrf-token"]') || {}).content || "",
        },
        body: JSON.stringify({ theme: next }),
      }).catch(function () { /* 未登录或接口未开放时静默 */ });
    });
  }
})();
