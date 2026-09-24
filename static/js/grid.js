/* The capture grid's keyboard: the whole ten-minute target lives here.
 *
 * Arrow keys move between cells. Enter and Tab advance (Shift reverses).
 * Typing replaces a cell's contents. A cell saves itself through HTMX when its
 * value changes - on leaving it - and the server answers with the cell
 * re-rendered, saved or refused. Nothing here needs the mouse.
 *
 * A save the server never answered (the network, a 500) is marked on the cell
 * it belongs to, so it is never silently lost. Plain JavaScript, no framework.
 */
(function () {
  "use strict";
  const table = document.querySelector("table.grid");
  if (!table) return;

  function cells() {
    return Array.from(table.querySelectorAll("tbody tr")).map(
      (tr) => Array.from(tr.querySelectorAll("td.cell input"))
    );
  }
  function position(input) {
    const grid = cells();
    for (let r = 0; r < grid.length; r++) {
      const c = grid[r].indexOf(input);
      if (c !== -1) return { grid, r, c };
    }
    return null;
  }
  function focusAt(grid, r, c) {
    if (r < 0 || r >= grid.length) return;
    const row = grid[r];
    if (c < 0 || c >= row.length) return;
    row[c].focus();
    row[c].select();
  }

  table.addEventListener("focusin", (event) => {
    const input = event.target;
    if (input.matches("td.cell input")) {
      input.dataset.original = input.value;
      input.select();
    }
  });

  table.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!input.matches("td.cell input")) return;
    const at = position(input);
    if (!at) return;
    const { grid, r, c } = at;
    const width = grid[r].length;
    switch (event.key) {
      case "ArrowRight": event.preventDefault(); focusAt(grid, r, c + 1); break;
      case "ArrowLeft": event.preventDefault(); focusAt(grid, r, c - 1); break;
      case "ArrowDown": event.preventDefault(); focusAt(grid, r + 1, c); break;
      case "ArrowUp": event.preventDefault(); focusAt(grid, r - 1, c); break;
      case "Enter":
        event.preventDefault();
        if (event.shiftKey) focusAt(grid, c > 0 ? r : r - 1, c > 0 ? c - 1 : width - 1);
        else focusAt(grid, c < width - 1 ? r : r + 1, c < width - 1 ? c + 1 : 0);
        break;
      case "Escape":
        input.value = input.dataset.original || "";
        break;
    }
  });

  /* Keep the keyboard where it was when a cell is swapped under it. */
  let refocus = null;
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    const td = event.detail.target;
    if (td && td.matches && td.matches("td.cell") && td.contains(document.activeElement)) {
      const at = position(document.activeElement);
      refocus = at ? [at.r, at.c] : null;
    } else {
      refocus = null;
    }
  });
  document.body.addEventListener("htmx:afterSwap", () => {
    if (refocus) { focusAt(cells(), refocus[0], refocus[1]); refocus = null; }
  });

  /* A save nobody answered is marked on its own cell, never lost quietly. */
  function unsaved(event) {
    const source = event.detail.elt;
    const td = source && source.closest ? source.closest("td.cell") : null;
    if (!td) return;
    td.classList.add("failed");
    let note = td.querySelector(".err");
    if (!note) {
      note = document.createElement("span");
      note.className = "err";
      note.setAttribute("role", "alert");
      td.appendChild(note);
    }
    note.textContent = "Not saved - the server did not answer. Retype to try again.";
    source.setAttribute("aria-invalid", "true");
  }
  document.body.addEventListener("htmx:responseError", unsaved);
  document.body.addEventListener("htmx:sendError", unsaved);
})();
