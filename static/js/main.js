document.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;

  const id = btn.getAttribute("data-id");
  const action = btn.getAttribute("data-action");
  if (!id || !action) return;

  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Please wait...";

  try {
    let url = "";
    if (action === "start") url = `/start/${id}`;
    else if (action === "stop") url = `/stop/${id}`;
    else if (action === "delete") {
      if (!confirm("Delete this bot and remove files? This cannot be undone.")) {
        btn.disabled = false; btn.textContent = origText; return;
      }
      url = `/delete/${id}`;
    } else return;

    const res = await fetch(url, { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      alert(data.msg || "Action failed");
    }
    window.location.reload();
  } catch (err) {
    alert("Network error");
    console.error(err);
    btn.disabled = false;
    btn.textContent = origText;
  }
});
