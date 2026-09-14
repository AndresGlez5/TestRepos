const form = document.querySelector("#login-form");
const password = document.querySelector("#password");
const button = document.querySelector("#login-button");
const error = document.querySelector("#login-error");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  button.disabled = true;
  button.querySelector("span:first-child").textContent = "Opening…";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not sign in.");
    window.location.replace("/");
  } catch (reason) {
    error.textContent = reason.message || "Could not sign in.";
    error.hidden = false;
    password.select();
  } finally {
    button.disabled = false;
    button.querySelector("span:first-child").textContent = "Open downloader";
  }
});
