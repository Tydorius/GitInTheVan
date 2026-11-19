"""Built-in cantrip templates that users can install with one click."""

TEMPLATES = [
    {
        "name": "Simple Dice Roller",
        "description": "Intercepts /roll commands (e.g. /roll 2d6+3) and injects results for the LLM to narrate. Supports standard polyhedral dice, modifiers, and exploding dice.",
        "hook_type": "pre",
        "execution_order": 10,
        "timeout_ms": 5000,
        "code": """context.character = context.character || {};
context.character.scenario = context.character.scenario || "";

const userMessage = (context.chat.last_message || "").toLowerCase();
const messages = context.chat.last_messages || [];
const llmMessage = messages.length >= 2 ? (messages[messages.length - 2].message || "") : "";

function rollDie(faces, explode) {
    let total = 0;
    let rolls = [];
    let current;
    do {
        current = Math.floor(Math.random() * faces) + 1;
        rolls.push(current);
        total += current;
    } while (explode && current === faces && faces > 1);
    return { total, rolls };
}

function executeRoll(count, faces, modifier, explode) {
    let results = [];
    let sum = 0;
    for (let i = 0; i < count; i++) {
        const r = rollDie(faces, explode);
        results.push(r);
        sum += r.total;
    }
    const final = sum + modifier;
    const rollStr = results.map(r => r.rolls.length > 1 ? '[' + r.rolls.join(', ') + ']' : '[' + r.rolls[0] + ']').join(' + ');
    const modStr = modifier !== 0 ? ' ' + (modifier > 0 ? '+' : '') + modifier : '';
    return { rollStr, modStr, final, sum };
}

let output = '\\n\\n[DICE SYSTEM]\\nThe player can roll dice using /roll commands. When results appear below, narrate them naturally.\\n';
let userResults = [];
const userRegex = /\\/roll\\s+(\\d+)d(\\d+)(!)?(?:\\s*([+-])\\s*(\\d+))?/gi;
let uMatch;
let uIndex = 1;

while ((uMatch = userRegex.exec(userMessage)) !== null) {
    const count = parseInt(uMatch[1]) || 1;
    const faces = parseInt(uMatch[2]) || 6;
    const explode = !!uMatch[3];
    let modifier = 0;
    if (uMatch[4] && uMatch[5]) {
        modifier = parseInt(uMatch[5]);
        if (uMatch[4] === '-') modifier *= -1;
    }
    const result = executeRoll(count, faces, modifier, explode);
    userResults.push('- User Roll ' + uIndex + ' (' + uMatch[0].trim() + '):\\n  > ' + result.rollStr + result.modStr + ' = ' + result.final);
    uIndex++;
}

if (userResults.length > 0) {
    output += '\\n[SYSTEM: DICE RESOLUTION]\\n';
    output += 'USER ROLLS:\\n' + userResults.join('\\n') + '\\n';
    output += '[SYSTEM: Map these results to the user\\'s actions. Narrate the outcome.]';
    context.character.scenario += output;
    console.log('Dice roller: ' + userResults.length + ' user rolls processed');
}""",
    },
    {
        "name": "Status Tracker",
        "description": "Instructs the LLM to emit status tags at the end of responses. Tracks location, time of day, and mood in chat_data across cycles. Strips the tags from visible context.",
        "hook_type": "pre",
        "execution_order": 10,
        "timeout_ms": 5000,
        "code": """context.character = context.character || {};
context.character.scenario = context.character.scenario || "";

const location = context.chat_data.get('location') || 'Unknown';
const timeOfDay = context.chat_data.get('time_of_day') || 'Morning';
const mood = context.chat_data.get('mood') || 'Neutral';

context.character.scenario += '\\n\\n[CURRENT STATUS]\\nLocation: ' + location + '\\nTime: ' + timeOfDay + '\\nMood: ' + mood + '\\n[/CURRENT STATUS]\\n';

context.character.scenario += '\\n[STATUS INSTRUCTIONS]\\nAt the very end of your response, you MUST include a status update block in this exact format (it will be hidden from the user):\\n<status>location=New Location|time=New Time|mood=New Mood</status>\\nUpdate the values if they change during this response. Keep them the same if nothing changes.\\n[/STATUS INSTRUCTIONS]';

const messages = context.chat.last_messages || [];
for (let i = messages.length - 1; i >= Math.max(0, messages.length - 5); i--) {
    const text = (messages[i] && messages[i].message) || '';
    const match = text.match(/<status>location=(.+?)\\|time=(.+?)\\|mood=(.+?)<\\/status>/i);
    if (match) {
        context.chat_data.set('location', match[1].trim());
        context.chat_data.set('time_of_day', match[2].trim());
        context.chat_data.set('mood', match[3].trim());
        console.log('Status tracker: Updated from LLM output - loc=' + match[1].trim() + ' time=' + match[2].trim());
        break;
    }
}""",
    },
    {
        "name": "Day Counter",
        "description": "Tracks a persistent day counter across conversation cycles using chat_data. Increments each message and injects the current day into the scenario.",
        "hook_type": "pre",
        "execution_order": 5,
        "timeout_ms": 3000,
        "code": """context.character = context.character || {};
context.character.scenario = context.character.scenario || "";

let day = context.chat_data.get('day') || 1;
let messageCount = context.chat_data.get('message_count') || 0;

messageCount++;
if (messageCount % 4 === 0) {
    day++;
}
context.chat_data.set('day', day);
context.chat_data.set('message_count', messageCount);

context.character.scenario += '\\n\\n[TIME TRACKING]\\nCurrent Day: ' + day + '\\nThis is day ' + day + ' of the story. Reference the passage of time naturally.\\n[/TIME TRACKING]';

console.log('Day counter: Day ' + day + ', Message ' + messageCount);""",
    },
    {
        "name": "Weather System",
        "description": "Rotates weather descriptions based on chat_data state. Changes weather every few messages for dynamic atmosphere.",
        "hook_type": "pre",
        "execution_order": 15,
        "timeout_ms": 3000,
        "code": """context.character = context.character || {};
context.character.scenario = context.character.scenario || "";

const weatherOptions = [
    { name: 'Clear', desc: 'The sky is clear and bright.' },
    { name: 'Cloudy', desc: 'Thick clouds blanket the sky.' },
    { name: 'Rain', desc: 'Rain falls steadily from above.' },
    { name: 'Storm', desc: 'Thunder rumbles and lightning flashes across the dark sky.' },
    { name: 'Snow', desc: 'Snow drifts down gently, blanketing everything in white.' },
    { name: 'Fog', desc: 'Dense fog obscures visibility in every direction.' },
    { name: 'Wind', desc: 'Strong winds whip through the area, carrying dust and leaves.' },
];

let weatherIndex = context.chat_data.get('weather_index') || 0;
let cyclesSinceChange = context.chat_data.get('weather_cycles') || 0;

cyclesSinceChange++;
if (cyclesSinceChange >= 5) {
    let newIndex;
    do {
        newIndex = Math.floor(Math.random() * weatherOptions.length);
    } while (newIndex === weatherIndex && weatherOptions.length > 1);
    weatherIndex = newIndex;
    cyclesSinceChange = 0;
    console.log('Weather system: Changed to ' + weatherOptions[weatherIndex].name);
}

context.chat_data.set('weather_index', weatherIndex);
context.chat_data.set('weather_cycles', cyclesSinceChange);

const weather = weatherOptions[weatherIndex];
context.character.scenario += '\\n\\n[WEATHER]\\n' + weather.desc + '\\n[/WEATHER]';""",
    },
    {
        "name": "Dynamic Lorebook Creator",
        "description": "Files <memstore key=\"name\">content</memstore> tags from the response into this chat's dynamic lorebook (context.lorebook), as keyword-triggered entries that surface when mentioned, and strips the tags from the visible reply. Entries take effect from this turn on. If you also use the built-in memory system, both will record the same tags -- usually you want one or the other.",
        "hook_type": "post",
        "run_pre_driver": False,
        "run_pre_navigator": True,
        "execution_order": 10,
        "timeout_ms": 5000,
        "code": """// Dynamic Lorebook Creator: memstore tags -> dynamic lorebook entries.
// Runs post-Driver (pre-Navigator): reads the response, files entries via
// context.lorebook, and strips the tags before the reader sees the reply.
// add() upserts by name, so a re-mentioned fact replaces its entry instead
// of duplicating it.
const content = (context.response && context.response.content) || "";
if (content) {
    const lb = context.lorebook.create({
        name: "Chat Notes",
        description: "Auto-filed from memstore tags by the Dynamic Lorebook Creator template."
    });
    if (lb) {
        const tagRe = /<memstore\\s+key=["']([^"']+)["']\\s*>([\\s\\S]*?)<\\/memstore>|<memstore\\s+key=["']([^"']+)["']\\s+value=["']([^"']*)["']\\s*\\/>/gi;
        let m, filed = 0;
        while ((m = tagRe.exec(content)) !== null) {
            const key = (m[1] || m[3] || "").trim();
            const value = (m[2] !== undefined ? m[2] : (m[4] || "")).trim();
            if (!key || !value) continue;
            lb.add({
                name: key.slice(0, 128),
                keys: [key.toLowerCase()],
                content: value,
                position: "before_last_message",
                is_constant: false
            });
            filed++;
        }
        if (filed) console.log("Dynamic Lorebook Creator: filed " + filed + " entry/entries into Chat Notes");
    } else {
        console.log("Dynamic Lorebook Creator: context.lorebook unavailable");
    }

    const stripped = content
        .replace(/<memstore\\s+key=["'][^"']+["']\\s*>[\\s\\S]*?<\\/memstore>/gi, "")
        .replace(/<memstore\\s+key=["'][^"']+["']\\s+value=["'][^"']*["']\\s*\\/>/gi, "")
        .replace(/\\n{3,}/g, "\\n\\n")
        .trim();
    if (stripped !== content) {
        context.response.content = stripped;
        context.response.modified = true;
    }
}""",
    },
    {
        "name": "Dynamic Lorebook RAG Executor",
        "description": "Companion to the Dynamic Lorebook Creator: periodically condenses this chat's dynamic lorebook with one Passenger call -- merges duplicate entries, tightens wording, sharpens keywords -- so injections stay small and relevant. Fires only after the book grows (default: 800 new characters since the last pass), roughly one LLM call every few turns. Requires Passenger enabled and a tool_use endpoint; when Passenger is off or a call fails, nothing happens that turn. The engine keeps injecting as usual -- this script only maintains the book.",
        "hook_type": "post",
        "run_pre_driver": False,
        "run_pre_navigator": True,
        "execution_order": 20,
        "timeout_ms": 10000,
        "code": """// Dynamic Lorebook RAG Executor (Phase 20b): keeps the chat's dynamic
// lorebook tight. Runs post-Driver after the Creator (order 20 vs its 10):
// searches the book for entries relevant to the newest message, makes ONE
// Passenger call that merges duplicates and tightens wording, and writes the
// result back through context.lorebook. The engine injects the maintained
// entries on later turns; this script never injects anything itself (the
// engine's injection would then double it). A valid model verdict (even an
// empty "nothing to improve") advances the throttle; a failed or unparseable
// call does not, so the next qualifying turn retries.
// Tunables -- edit freely after install, this code is yours.
const BOOK_NAME = "Chat Notes";   // book to maintain (the Creator's default)
const MIN_ENTRIES = 3;            // never spend a call on a tiny book
const MIN_GROWTH = 800;           // new characters required since the last pass
const MAX_ENTRIES = 12;           // entries sent to the model per pass
const MAX_CHARS = 1500;           // per-entry character cap, both directions

if (context.passenger.enabled) {
    const lb = context.lorebook.get(BOOK_NAME);
    const entries = lb ? lb.entries() : [];
    const size = entries.reduce((n, e) => n + (e.content || "").length, 0);
    const doneAt = context.chat_data.get("rag_last_size") || 0;
    if (entries.length >= MIN_ENTRIES && size - doneAt >= MIN_GROWTH) {
        // Retrieval: significant words from the newest message pick which
        // entries this pass touches. The model does the real ranking; the
        // keywords only bound the payload.
        const msgs = context.chat.last_messages || [];
        const text = msgs.length
            ? String(msgs[msgs.length - 1].message || "")
            : String(context.chat.last_message || "");
        const STOP = new Set(["about", "after", "again", "their", "there",
            "them", "they", "this", "that", "what", "when", "where", "which",
            "while", "with", "would", "your", "have", "just", "know", "like",
            "over", "some", "take", "than", "then", "them", "were", "will",
            "into", "very", "come", "could", "does", "each", "even", "from",
            "gone", "here", "much", "must", "never", "said", "same", "shall",
            "should", "still", "such", "sure", "through", "under", "want"]);
        const words = [];
        for (const w of (text.toLowerCase().match(/[a-z]{4,}/g) || [])) {
            if (!STOP.has(w) && words.indexOf(w) === -1) words.push(w);
            if (words.length >= 12) break;
        }
        const hits = words.length ? lb.search(words.join(" ")).slice(0, MAX_ENTRIES) : [];
        if (hits.length) {
            const recent = msgs.slice(-2)
                .map(m => String(m.message || "")).join("\\n")
                .slice(-1200);
            const reply = await context.passenger.call({
                tag: "tool_use",
                instructions: 'You maintain a roleplay lorebook. The input lists lorebook entries as JSON. Merge entries that overlap, tighten verbose wording, and sharpen keywords, preserving every fact. Reply with ONLY a JSON array; each element is an entry you changed or merged: {"name": string, "keys": string[], "content": string, "replaces": string[]} where "replaces" names the input entries this one supersedes. Return [] if nothing deserves changing. Never invent facts.',
                context: JSON.stringify({
                    entries: hits.map(e => ({
                        name: e.name || "",
                        keys: e.keys || [],
                        content: String(e.content || "").slice(0, MAX_CHARS),
                    })),
                    recent: recent,
                }),
                model: "",
            });
            // Reply parsing: strip code fences, take the outermost JSON
            // array, validate shape. Anything else is a no-op this turn.
            let changes = null;
            if (typeof reply === "string" && reply.trim()) {
                const stripped = reply.replace(/```[a-z]*\\s*/gi, "").replace(/```/g, "");
                const start = stripped.indexOf("[");
                const end = stripped.lastIndexOf("]");
                if (start !== -1 && end > start) {
                    try { changes = JSON.parse(stripped.slice(start, end + 1)); }
                    catch (err) { changes = null; }
                }
            }
            if (Array.isArray(changes)) {
                const byName = {};
                for (const e of entries) byName[(e.name || "").toLowerCase()] = e;
                let adds = 0, dels = 0;
                for (const c of changes.slice(0, MAX_ENTRIES)) {
                    if (!c || typeof c !== "object") continue;
                    const name = String(c.name || "").trim().slice(0, 128);
                    const content = String(c.content || "").trim().slice(0, MAX_CHARS);
                    if (!name || !content) continue;
                    const keys = (Array.isArray(c.keys) && c.keys.length ? c.keys : [name])
                        .map(k => String(k).trim().toLowerCase())
                        .filter(k => k).slice(0, 8);
                    const rep = Array.isArray(c.replaces)
                        ? c.replaces.map(r => String(r).trim()).filter(r => r) : [];
                    // A rewrite of exactly one entry inherits its position
                    // and order; a merge takes the Creator's defaults.
                    let position = "before_last_message";
                    let order = 10;
                    if (rep.length === 1) {
                        const orig = byName[rep[0].toLowerCase()];
                        if (orig) {
                            if (orig.position) position = orig.position;
                            if (Number.isFinite(orig.insertion_order)) order = orig.insertion_order;
                        }
                    }
                    lb.add({
                        name: name, keys: keys, content: content,
                        position: position, insertion_order: order,
                        is_constant: false,
                    });
                    adds++;
                    for (const r of rep) {
                        if (r.toLowerCase() === name.toLowerCase()) continue;
                        lb.delete(r);
                        dels++;
                    }
                }
                const newSize = lb.entries()
                    .reduce((n, e) => n + (e.content || "").length, 0);
                context.chat_data.set("rag_last_size", newSize);
                console.log("RAG Executor: " + adds + " rewritten, " + dels
                    + " merged away; book now " + newSize + " chars");
            } else {
                console.log("RAG Executor: no usable reply this turn; "
                    + "throttle not advanced");
            }
        }
    }
}""",
    },
]


def get_templates():
    return TEMPLATES
