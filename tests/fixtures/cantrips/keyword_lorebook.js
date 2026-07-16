// Fixture cantrip: keyword-gated lorebook + dice-command parsing.
// Exercises the load-bearing behaviors of a realistic JanitorAI-style script
// (conditional injection on keyword match, regex command parsing, debug logging)
// without depending on any path outside this repository.

const LORE_ENTRIES = {
  "example kingdom": "Example Kingdom: a vast realm ruled by a wise council.",
  "crystal tower": "Crystal Tower: an ancient spire humming with arcane energy.",
  "dragon": "Dragons: mighty beasts said to sleep beneath the mountains.",
};

const ROLL_REGEX = /^\/roll\s+(\d+)d(\d+)(?:([+-])\s*(\d+))?$/i;

context.character.scenario = context.character.scenario || "";

// 1. Keyword-gated lore injection (match + no-match paths).
const lastLower = (context.chat.last_message || "").toLowerCase();
let injected = false;
for (const [keyword, lore] of Object.entries(LORE_ENTRIES)) {
  if (lastLower.includes(keyword)) {
    context.character.scenario += "\n" + lore;
    injected = true;
  }
}

// 2. Dice-command parsing via regex (JanitorAI Dice_Controller pattern).
const rollMatch = (context.chat.last_message || "").match(ROLL_REGEX);
if (rollMatch) {
  const count = parseInt(rollMatch[1], 10);
  const sides = parseInt(rollMatch[2], 10);
  const mod = rollMatch[4] ? parseInt(rollMatch[4], 10) * (rollMatch[3] === "-" ? -1 : 1) : 0;
  const total = count * Math.floor(sides / 2 + 1) + mod;
  context.character.scenario += `\n[DICE SYSTEM] Rolled ${count}d${sides}${mod !== 0 ? (mod > 0 ? "+" : "") + mod : ""} = ${total}`;
}

// 3. Debug-log emission (PropertyExploration pattern).
console.log("keyword_lorebook: injected=" + injected + " roll=" + (rollMatch !== null));
