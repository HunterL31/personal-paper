/* A little vanilla JS: the Check buttons and the job pollers. No build step. */

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  try {
    return await response.json();
  } catch (err) {
    return { ok: false, error: "The paper did not answer properly (" + response.status + ")" };
  }
}

function show(el, text, bad) {
  if (!el) return;
  el.textContent = text;
  el.className = bad ? "result err" : "result";
}

/* Run `work()` while the button says "…", then print the outcome beside it. */
async function withButton(button, target, work, describe) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Checking…";
  show(target, "");
  try {
    const data = await work();
    if (data && data.ok) {
      show(target, describe(data.result), false);
    } else {
      show(target, (data && data.error) || "failed", true);
    }
    return data;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/* Poll /jobs/<id> once a second until it is done, then hand it to `onDone`. */
function pollJob(id, onUpdate, onDone) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    let job = null;
    try {
      const response = await fetch("/jobs/" + id);
      job = await response.json();
    } catch (err) {
      job = null;
    }
    if (job && (job.status === "done" || job.status === "error")) {
      stopped = true;
      onDone(job);
      return;
    }
    if (onUpdate) onUpdate(job);
    setTimeout(tick, 1000);
  };
  tick();
}
