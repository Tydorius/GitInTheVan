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
]


def get_templates():
    return TEMPLATES
