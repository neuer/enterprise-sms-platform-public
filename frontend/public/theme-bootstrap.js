try {
  document.documentElement.dataset.theme = window.localStorage.getItem("sms-theme") === "light" ? "light" : "dark"
} catch {
  document.documentElement.dataset.theme = "dark"
}
