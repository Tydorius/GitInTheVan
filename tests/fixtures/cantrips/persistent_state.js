// Fixture cantrip: persistent state across calls + multi-character handling.
// Exercises cantrip_data read/write (Hidden_Persistent_Memory pattern) and
// per-entity context building (Multiple_Character pattern) without depending
// on any path outside this repository.

// 1. Persistent hidden state via context.cantrip_data (survives across calls).
const seen = context.cantrip_data.get("visit_count") || 0;
context.cantrip_data.set("visit_count", seen + 1);
context.cantrip_data.set("last_scene", context.chat.last_message || "");

// 2. Multi-character context handling: branch on which entity the user mentions.
context.character.scenario = context.character.scenario || "";
const lastLower = (context.chat.last_message || "").toLowerCase();

const ENTITIES = {
  "character a": "Character A steps forward, blade drawn.",
  "character b": "Character B lingers in the shadows, watching.",
};

for (const [trigger, narration] of Object.entries(ENTITIES)) {
  if (lastLower.includes(trigger)) {
    context.character.scenario += "\n" + narration;
  }
}

console.log("persistent_state: visit_count=" + (seen + 1));
