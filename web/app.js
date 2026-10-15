"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const storageKey = "blackjack.finished-table.v1";
  const moneyFormat = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  const actionLabels = { hit: "Hit", stand: "Stand", double: "Double", split: "Split", surrender: "Surrender" };
  const actionKeys = { hit: "H", stand: "S", double: "D", split: "P", surrender: "R" };
  let state = null;
  let pending = false;
  let editingSettings = false;
  let pollTimer = null;
  let lastPromptId = null;
  let lastAnnounced = "";
  let savedTable = null;
  const playerDraft = Array.from({ length: 7 }, (_, i) => ({ name: `Player ${i + 1}`, cash: "1000.00" }));

  // All wager arithmetic uses integer cents. Decimals only cross the API boundary as strings.
  function cents(value) {
    if (typeof value !== "string" && typeof value !== "number") return null;
    const text = String(value).trim();
    if (!/^(?:\d+(?:\.\d{1,2})?|\.\d{1,2})$/.test(text)) return null;
    const [whole, fraction = ""] = text.split(".");
    const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
    return Number.isSafeInteger(amount) ? amount : null;
  }
  const decimal = (amount) => `${Math.floor(amount / 100)}.${String(amount % 100).padStart(2, "0")}`;
  const money = (value) => moneyFormat.format((cents(value) ?? 0) / 100);
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  function inlineError(id, message = "") {
    $(id).textContent = message;
    $(id).hidden = !message;
  }
  function announce(message) {
    if (message !== lastAnnounced) { $("live-status").textContent = message; lastAnnounced = message; }
  }
  function readPlayerFields() {
    $("player-fields").querySelectorAll(".player-field-row").forEach((row, i) => {
      playerDraft[i] = { name: row.querySelector("[data-name]").value, cash: row.querySelector("[data-cash]").value };
    });
  }
  function renderPlayerFields() {
    const fields = $("player-fields");
    fields.replaceChildren();
    const count = Number($("player-count").value);
    for (let i = 0; i < count; i += 1) {
      const row = node("div", "player-field-row");
      const name = document.createElement("input");
      name.type = "text"; name.id = `player-name-${i}`; name.maxLength = 40;
      name.setAttribute("aria-label", `Player ${i + 1} name`); name.dataset.name = "";
      name.value = playerDraft[i].name; name.autocomplete = "off";
      const cash = document.createElement("input");
      cash.type = "text"; cash.id = `player-cash-${i}`; cash.inputMode = "decimal";
      cash.setAttribute("aria-label", `Player ${i + 1} starting cash in dollars`); cash.dataset.cash = "";
      cash.value = playerDraft[i].cash; cash.autocomplete = "off";
      row.append(name, cash); fields.append(row);
    }
  }
  function copyStateToForm() {
    if (!state || !state.players.length) return;
    state.players.forEach((player, i) => { playerDraft[i] = { name: player.name, cash: player.cash }; });
    $("player-count").value = String(state.players.length);
    $("minimum-bet").value = state.limits.minimum;
    $("bet-increment").value = state.limits.increment;
    $("maximum-bet").value = state.limits.maximum;
    renderPlayerFields();
  }

  function renderCard(rank, index) {
    const card = node("div", `playing-card${rank === null ? " face-down" : ""}`);
    card.setAttribute("role", "img");
    card.classList.add(`card-delay-${Math.min(index, 4)}`);
    if (rank === null) {
      card.setAttribute("aria-label", "Face-down dealer card");
      const mark = node("span", "card-back-mark", "21"); mark.setAttribute("aria-hidden", "true"); card.append(mark);
    } else {
      card.setAttribute("aria-label", `Card ${rank}`);
      [node("span", "card-corner", rank), node("span", "card-center", rank), node("span", "card-corner card-corner-bottom", rank), node("span", "card-ornament", "◆")].forEach((part) => { part.setAttribute("aria-hidden", "true"); card.append(part); });
    }
    return card;
  }
  function outcomeStyle(outcome) {
    const text = (outcome || "").toLowerCase();
    if (text.includes("los") || text.includes("bust") || text.includes("surrender")) return "loss";
    if (text.includes("win") || text.includes("blackjack")) return "win";
    return "";
  }
  function renderTable() {
    $("round-number").textContent = state.round ? String(state.round).padStart(2, "0") : "—";
    $("table-limits").textContent = `${money(state.limits.minimum)} minimum · ${money(state.limits.maximum)} maximum`;
    const dealer = $("dealer-cards");
    dealer.replaceChildren();
    if (state.dealer.cards.length) state.dealer.cards.forEach((rank, i) => dealer.append(renderCard(rank, i)));
    else for (let i = 0; i < 2; i += 1) { const empty = node("div", "empty-card"); empty.setAttribute("aria-hidden", "true"); dealer.append(empty); }
    $("dealer-total").hidden = state.dealer.total === null;
    $("dealer-total").textContent = state.dealer.total === null ? "" : String(state.dealer.total);
    $("dealer-total").setAttribute("aria-label", `Dealer total ${state.dealer.total ?? "hidden"}`);
    $("empty-table").hidden = state.players.length > 0;
    const seats = $("player-seats"); seats.replaceChildren();
    state.players.forEach((player, i) => {
      const active = state.current_player === i && ["bet", "insurance", "play"].includes(state.phase);
      const seat = node("section", `player-seat${active ? " active-seat" : ""}`);
      seat.setAttribute("aria-label", `${player.name}, bankroll ${money(player.cash)}${active ? ", current player" : ""}`);
      const heading = node("div", "seat-heading");
      heading.append(node("span", "seat-number", i + 1), node("span", "seat-name", player.name)); seat.append(heading);
      const hands = node("div", "player-hands");
      player.hands.forEach((hand, h) => {
        const current = active && state.phase === "play" && state.current_hand === h;
        const handNode = node("div", `player-hand${current ? " active-hand" : ""}`);
        handNode.setAttribute("role", "group");
        handNode.setAttribute("aria-label", `${hand.split ? `Split hand ${h + 1}, ` : ""}total ${hand.total}, bet ${money(hand.bet)}${current ? ", current hand" : ""}`);
        if (current) handNode.setAttribute("aria-current", "true");
        const cards = node("div", "cards"); hand.cards.forEach((rank, c) => cards.append(renderCard(rank, c)));
        const bottom = node("div", "hand-bottom");
        if (hand.split) bottom.append(node("span", "split-label", `#${h + 1}`));
        bottom.append(node("span", "total-badge", hand.total), node("span", "wager-token", money(hand.bet)));
        const outcome = hand.outcome || (hand.surrendered ? "Surrendered" : hand.total > 21 ? "Bust" : current ? "Your move" : "");
        handNode.append(cards, bottom, node("span", `hand-outcome ${outcomeStyle(outcome)}`, outcome));
        hands.append(handNode);
      });
      if (!player.hands.length) hands.append(node("div", "seat-placeholder", state.phase === "bet" ? "Waiting for bets" : state.round > 0 ? "Sitting out" : "Ready for the deal"));
      seat.append(hands);
      const cash = node("p", "seat-cash", money(player.cash)); cash.append(node("span", "seat-cash-label", "BANKROLL")); seat.append(cash);
      seats.append(seat);
    });
  }

  function bettingNumbers() {
    const prompt = state?.prompt;
    return { min: cents(prompt?.minimum ?? state?.limits.minimum) ?? 0, step: cents(prompt?.increment ?? state?.limits.increment) ?? 1, max: cents(prompt?.max_stake) ?? 0 };
  }
  function snap(amount) {
    const { min, step, max } = bettingNumbers();
    if (amount <= 0 || max < min) return 0;
    return min + Math.floor((Math.max(min, Math.min(amount, max)) - min) / step) * step;
  }
  function setBet(amount) { $("bet-amount").value = decimal(snap(amount)); inlineError("bet-error"); updateBetLabel(); }
  function updateBetLabel() { $("place-bet").textContent = cents($("bet-amount").value) === 0 ? "Sit out this round" : "Place bet"; }
  function renderChips() {
    const { min, step } = bettingNumbers();
    const chips = $("chip-selector"); chips.replaceChildren();
    const options = [{ amount: min, label: "Min", description: "Set minimum bet", set: true }, { amount: step, label: `+${decimal(step).replace(/\.00$/, "")}`, description: `Add one step, ${money(decimal(step))}` }, ...[500, 2500, 10000].map((amount) => ({ amount, label: String(amount / 100), description: `Add ${money(decimal(amount))}` }))];
    options.forEach((option) => {
      const button = node("button", "chip-button", option.label); button.type = "button";
      button.setAttribute("aria-label", `${option.description}; adjusts to the permitted bet step`);
      button.addEventListener("click", () => setBet(option.set ? min : (cents($("bet-amount").value) ?? 0) + option.amount));
      chips.append(button);
    });
  }
  function renderControls() {
    const focusWasInside = $("controls").contains(document.activeElement);
    const phase = editingSettings || state.phase === "setup" ? "setup" : ["bet", "insurance", "play"].includes(state.phase) && !state.prompt ? "busy" : state.phase;
    ["setup", "bet", "insurance", "play", "result", "busy"].forEach((name) => { $(`${name}-controls`).hidden = name !== phase && !(name === "busy" && phase === "error"); });
    $("phase-badge").textContent = phase === "play" ? "YOUR MOVE" : phase.toUpperCase();
    $("configure-button").disabled = pending || !["setup", "result"].includes(state.phase);
    $("cancel-settings").hidden = !editingSettings || state.phase === "setup";
    $("restore-box").hidden = !savedTable || state.phase !== "setup";
    const player = state.players[state.current_player];
    const name = player?.name || "Player";
    const descriptions = {
      setup: ["WELCOME TO THE TABLE", "Make yourself at home.", editingSettings ? "Apply new bankrolls and limits to start a fresh table with a new shoe." : "Set the bankrolls and bet limits, then deal your first round."],
      bet: ["PLACE YOUR WAGER", `${name}, you’re up.`, "Choose an opening bet, or keep your bankroll and sit this round out."],
      insurance: ["DEALER SHOWS AN ACE", `${name}, insurance?`, "Decide whether to cover the dealer’s possible blackjack."],
      play: ["THE NEXT CARD IS YOUR CALL", `${name}, your move.`, state.current_hand !== null && player?.hands.length > 1 ? `Playing hand ${state.current_hand + 1} of ${player.hands.length}. The active hand is outlined in gold.` : "Beat the dealer without going over 21."],
      result: [state.round > 0 ? "THE ROUND IS COMPLETE" : "YOUR TABLE IS READY", state.round > 0 ? "On to the next hand." : "Let’s deal you in.", state.round > 0 ? "The wagers have settled. Your bankrolls are ready for another round." : "Everyone starts with a choice: place a bet or sit out."],
      busy: ["CARDS ON THE TABLE", "Dealing the next move.", "Watch the table while the dealer finishes the action."],
      error: ["TABLE PAUSED", "Let’s check the table.", state.message || "The server encountered an error. Review the message above."],
    };
    const description = descriptions[phase] || descriptions.busy;
    $("panel-eyebrow").textContent = description[0]; $("control-title").textContent = description[1]; $("control-description").textContent = description[2];
    if (phase === "bet") {
      const { min, step, max } = bettingNumbers();
      $("available-cash").textContent = money(state.prompt?.cash ?? player.cash);
      $("bet-help").textContent = max < min ? "Your bankroll is below the minimum. Sit out or change bankrolls after this round." : `${money(decimal(min))} minimum · ${money(decimal(step))} steps · ${money(decimal(max))} available maximum`;
      if (state.prompt.id !== lastPromptId) { setBet(min); renderChips(); inlineError("bet-error"); }
    }
    if (phase === "insurance" && state.prompt.id !== lastPromptId) {
      $("insurance-amount").value = "0.00";
      $("insurance-help").textContent = `Any amount from $0.00 to ${money(state.prompt.max_stake)}, in cents. This wager is separate from your opening bet.`;
      inlineError("insurance-error");
    }
    if (phase === "play") {
      const hand = player?.hands[state.current_hand];
      const summary = $("hand-summary"); summary.replaceChildren();
      if (hand) {
        const label = node("div", ""); label.append(node("div", "hand-summary-label", "YOUR HAND"), node("div", "hand-summary-bet", `${money(hand.bet)} on the table`));
        summary.append(label, node("strong", "hand-summary-total", hand.total));
      }
      const actions = $("action-buttons"); actions.replaceChildren();
      (state.prompt.actions || []).forEach((action) => {
        const button = node("button", `button action-button ${action === "hit" ? "button-gold" : "button-outline"}`, actionLabels[action] || action);
        button.type = "button"; button.dataset.serverAction = ""; button.dataset.playAction = action;
        button.setAttribute("aria-keyshortcuts", actionKeys[action] || "");
        if (actionKeys[action]) { const shortcut = node("kbd", "", actionKeys[action]); shortcut.setAttribute("aria-hidden", "true"); button.append(shortcut); }
        button.addEventListener("click", () => sendAction(action)); actions.append(button);
      });
    }
    if (phase === "result") {
      const summary = $("result-summary"); summary.replaceChildren();
      state.players.forEach((person) => {
        const line = node("div", "result-player"); line.append(node("span", "", person.name), node("strong", "", money(person.cash))); summary.append(line);
      });
      $("new-round").firstChild.textContent = state.round > 0 ? "Deal next round " : "Deal first round ";
      if (!canAnyoneBet()) {
        $("control-title").textContent = "Time to refresh the table.";
        $("control-description").textContent = "Every bankroll is below the minimum bet. Open Table settings to change the bankrolls or lower the minimum.";
      } else if (state.message && state.round > 0 && !state.players.some((person) => person.hands.length)) {
        $("control-description").textContent = state.message;
      }
    }
    lastPromptId = state.prompt?.id ?? null;
    syncPending();
    announce(`${description[1]} ${phase === "play" ? `Available actions: ${(state.prompt?.actions || []).join(", ")}.` : ""}`);
    if (focusWasInside) {
      const target = { setup: $("player-count"), bet: $("bet-amount"), insurance: $("insurance-amount"), play: $("action-buttons").querySelector("button"), result: $("new-round") }[phase];
      if (target && !target.disabled) target.focus({ preventScroll: true });
      else $("controls").focus({ preventScroll: true });
    }
  }
  function canAnyoneBet() {
    const min = cents(state?.limits.minimum);
    return min !== null && Boolean(state?.players.some((player) => cents(player.cash) >= min));
  }
  function syncPending() {
    document.querySelectorAll("[data-server-action]").forEach((button) => { button.disabled = pending || !state; });
    $("new-round").disabled = pending || !state || !canAnyoneBet();
    $("configure-button").disabled = pending || !state || !["setup", "result"].includes(state.phase);
    $("controls").setAttribute("aria-busy", String(pending));
  }
  function persistFinishedTable() {
    if (state.phase !== "result" || state.round < 1) return;
    const saved = { version: 1, players: state.players.map(({ name, cash }) => ({ name, cash })), limits: state.limits };
    try { localStorage.setItem(storageKey, JSON.stringify(saved)); savedTable = saved; } catch (_) { /* Storage may be disabled. The game still works. */ }
  }
  function acceptState(next) {
    if (!next || typeof next.revision !== "number") throw new Error("The server returned an invalid table state.");
    if (state && next.revision < state.revision) return;
    const changed = !state || state.revision !== next.revision;
    state = next;
    if (changed) { renderTable(); renderControls(); persistFinishedTable(); }
    if (state.phase === "error") inlineError("error-banner", state.message || "The table encountered an error.");
  }
  async function request(path, body) {
    if (pending) return false;
    pending = true; syncPending(); inlineError("error-banner");
    try {
      const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
      if (path === "/api/new") editingSettings = false;
      acceptState(data);
      return true;
    } catch (error) { inlineError("error-banner", error.message || "Unable to reach the local game server."); return false; }
    finally { pending = false; syncPending(); schedulePoll(); }
  }
  async function poll() {
    if (pending) { schedulePoll(); return; }
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`Unable to read table state (${response.status}).`);
      const next = await response.json();
      // A POST could have started while this GET was in flight. Ignore older snapshots.
      if (!state || next.revision >= state.revision) acceptState(next);
      if ($("error-banner").dataset.connectionError) { inlineError("error-banner"); delete $("error-banner").dataset.connectionError; }
    } catch (error) { inlineError("error-banner", `${error.message || "Cannot connect to the game server."} Retrying automatically.`); $("error-banner").dataset.connectionError = "true"; }
    finally { schedulePoll(); }
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    const active = state && ["busy", "bet", "insurance", "play"].includes(state.phase);
    pollTimer = setTimeout(poll, active ? 250 : 2000);
  }
  function sendAction(value) {
    if (!state?.prompt || pending) return;
    request("/api/action", { prompt_id: state.prompt.id, value });
  }

  $("player-count").addEventListener("change", () => { readPlayerFields(); renderPlayerFields(); });
  $("setup-form").addEventListener("submit", (event) => {
    event.preventDefault(); if (pending) return;
    readPlayerFields(); inlineError("setup-error");
    const min = cents($("minimum-bet").value), step = cents($("bet-increment").value), max = cents($("maximum-bet").value);
    if (min === null || step === null || max === null || min <= 0 || step <= 0 || max < min) { inlineError("setup-error", "Enter positive bet limits in cents. Maximum must be at least the minimum."); return; }
    if (min % 2 || step % 2) { inlineError("setup-error", "Minimum and step must be multiples of $0.02 so half-bet payouts stay exact."); return; }
    const players = playerDraft.slice(0, Number($("player-count").value));
    if (players.some((player) => !player.name.trim() || cents(player.cash) === null)) { inlineError("setup-error", "Give each player a name and a nonnegative starting bankroll in dollars and cents."); return; }
    request("/api/new", { players: players.map((player) => ({ name: player.name.trim(), cash: decimal(cents(player.cash)) })), limits: { minimum: decimal(min), increment: decimal(step), maximum: decimal(max) } });
  });
  $("configure-button").addEventListener("click", () => {
    if (!state || !["setup", "result"].includes(state.phase)) return;
    if (!editingSettings) copyStateToForm();
    editingSettings = true; renderControls(); $("player-count").focus();
  });
  $("cancel-settings").addEventListener("click", () => { editingSettings = false; inlineError("setup-error"); renderControls(); });
  $("new-round").addEventListener("click", () => request("/api/round", {}));
  $("bet-amount").addEventListener("input", () => { inlineError("bet-error"); updateBetLabel(); });
  $("bet-increase").addEventListener("click", () => { const { min, step } = bettingNumbers(); const current = cents($("bet-amount").value) ?? 0; setBet(current === 0 ? min : current + step); });
  $("bet-decrease").addEventListener("click", () => { const { min, step } = bettingNumbers(); const current = cents($("bet-amount").value) ?? 0; setBet(current <= min ? 0 : current - step); });
  $("clear-bet").addEventListener("click", () => setBet(0));
  $("max-bet").addEventListener("click", () => setBet(bettingNumbers().max));
  function submitBet() {
    if (state?.phase !== "bet") return;
    const amount = cents($("bet-amount").value); const { min, step, max } = bettingNumbers();
    if (amount === null || (amount !== 0 && (amount < min || amount > max || (amount - min) % step !== 0))) { inlineError("bet-error", `Choose $0 to sit out, or ${money(decimal(min))} plus whole ${money(decimal(step))} steps, up to ${money(decimal(max))}.`); return; }
    inlineError("bet-error"); sendAction(decimal(amount));
  }
  $("place-bet").addEventListener("click", submitBet);
  $("bet-amount").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); submitBet(); } });
  $("sit-out").addEventListener("click", () => sendAction("0.00"));
  $("insurance-zero").addEventListener("click", () => { $("insurance-amount").value = "0.00"; inlineError("insurance-error"); });
  $("insurance-max").addEventListener("click", () => { $("insurance-amount").value = state.prompt.max_stake; inlineError("insurance-error"); });
  function submitInsurance() {
    if (state?.phase !== "insurance") return;
    const amount = cents($("insurance-amount").value), max = cents(state.prompt.max_stake);
    if (amount === null || amount > max) { inlineError("insurance-error", `Enter an amount from $0.00 to ${money(state.prompt.max_stake)}, in cents.`); return; }
    inlineError("insurance-error"); sendAction(decimal(amount));
  }
  $("place-insurance").addEventListener("click", submitInsurance);
  $("insurance-amount").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); submitInsurance(); } });
  document.addEventListener("keydown", (event) => {
    if (event.repeat || event.altKey || event.ctrlKey || event.metaKey || pending || editingSettings || state?.phase !== "play") return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const action = Object.keys(actionKeys).find((name) => actionKeys[name].toLowerCase() === event.key.toLowerCase());
    if (action && state.prompt.actions.includes(action)) { event.preventDefault(); sendAction(action); }
  });
  $("restore-button").addEventListener("click", () => {
    if (!savedTable || state?.phase !== "setup") return;
    savedTable.players.forEach((player, i) => { playerDraft[i] = { name: player.name, cash: player.cash }; });
    $("player-count").value = String(savedTable.players.length); renderPlayerFields();
    $("minimum-bet").value = savedTable.limits.minimum; $("bet-increment").value = savedTable.limits.increment; $("maximum-bet").value = savedTable.limits.maximum;
    announce("Saved bankrolls and settings loaded. Create the table to begin with a fresh shoe.");
  });
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (saved?.version === 1 && Array.isArray(saved.players) && saved.players.length >= 1 && saved.players.length <= 7 && saved.players.every((player) => typeof player.name === "string" && cents(player.cash) !== null) && saved.limits && ["minimum", "increment", "maximum"].every((key) => cents(saved.limits[key]) !== null)) savedTable = saved;
  } catch (_) { /* Ignore absent or invalid local data. */ }
  renderPlayerFields(); syncPending(); poll();
})();
