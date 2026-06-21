const __debugLogs = [];
console.log = (...args) => {
    __debugLogs.push(args.map(a => {
        if (typeof a === 'object') {
            try { return JSON.stringify(a); } catch { return String(a); }
        }
        return String(a);
    }).join(' '));
};
console.error = console.warn = console.log;

const context = JSON.parse(atob("__GITV_CONTEXT__"));
const __chatData = JSON.parse(atob("__GITV_CHATDATA__"));
const __userCode = atob("__GITV_USERCODE__");

context.chat_data = {
    get: (key) => key in __chatData ? __chatData[key] : null,
    set: (key, value) => { __chatData[key] = value; },
    keys: () => Object.keys(__chatData),
    delete: (key) => { delete __chatData[key]; }
};

if (!context.character) context.character = {};
if (!context.character.personality) context.character.personality = "";
if (!context.character.scenario) context.character.scenario = "";
if (!context.character.example_dialogs) context.character.example_dialogs = "";

let __hasResponse = false;
if (context.response) {
    __hasResponse = true;
    if (typeof context.response.content !== 'string') context.response.content = "";
    if (typeof context.response.original_content !== 'string') context.response.original_content = context.response.content;
    if (typeof context.response.modified !== 'boolean') context.response.modified = false;
}

let __hasToolCall = false;
if (context.tool_call) {
    __hasToolCall = true;
}
if (typeof context.tool_result !== 'string') context.tool_result = "";

let __error = null;
try {
    let fn;
    try {
        fn = new Function('context', __userCode);
    } catch (e1) {
        const trimmed = __userCode.replace(/\n\s*\}\s*$/, '');
        fn = new Function('context', trimmed);
    }
    fn(context);
} catch (e) {
    __error = e.message || String(e);
    __debugLogs.push('SCRIPT ERROR: ' + __error);
}

const __result = {
    personality: context.character.personality || "",
    scenario: context.character.scenario || "",
    example_dialogs: context.character.example_dialogs || "",
    response_content: __hasResponse ? (context.response.content || null) : null,
    tool_result: context.tool_result || "",
    chat_data: Object.assign({}, __chatData),
    debug_logs: __debugLogs,
    error: __error
};

Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify(__result)));
