// Agency settings: live preview of the ACORD agency box, completeness count, unsaved-changes bar.
const form = document.getElementById("agencyform");
const inputs = [...form.querySelectorAll("input[data-preview]")];
const bar = document.getElementById("savebar");
const initial = new Map(inputs.map((i) => [i, i.value]));

function render() {
  inputs.forEach((i) => document.querySelectorAll(`[data-show="${i.name}"]`).forEach((el) => {
    el.textContent = i.value.trim();
    el.classList.toggle("filled", !!i.value.trim());
  }));
  const done = inputs.filter((i) => i.value.trim()).length;
  document.getElementById("complete").textContent = `${done} of ${inputs.length} agency details filled in`;
  bar.hidden = !inputs.some((i) => i.value !== initial.get(i));
}
inputs.forEach((i) => i.addEventListener("input", () => {
  if (i.classList.contains("upper")) i.value = i.value.toUpperCase();
  render();
}));
form.addEventListener("reset", () => setTimeout(render));
window.addEventListener("beforeunload", (e) => { if (!bar.hidden) { e.preventDefault(); e.returnValue = ""; } });
form.addEventListener("submit", () => (bar.hidden = true));
render();
