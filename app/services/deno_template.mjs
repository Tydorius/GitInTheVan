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

// Base64 payloads are UTF-8 bytes. atob() alone yields a Latin-1 "binary
// string" (one char per byte), which mojibakes every multi-byte character --
// curly quotes and em-dashes in cantrip code, accents and emoji in stored
// data. Decode the byte string through TextDecoder instead.
const __b64 = (s) => new TextDecoder().decode(Uint8Array.from(atob(s), c => c.charCodeAt(0)));

const context = JSON.parse(__b64("__GITV_CONTEXT__"));
const __chatData = JSON.parse(__b64("__GITV_CHATDATA__"));
const __userData = JSON.parse(__b64("__GITV_USERDATA__"));
const __cantripData = JSON.parse(__b64("__GITV_CANTRIPDATA__"));
const __userCode = __b64("__GITV_USERCODE__");

// 0.42.0: delete() must persist. The facades record deleted keys so the
// host save step can remove the rows -- the final dict alone cannot
// express "this key is gone" (absence once meant "untouched", which is
// why deletes used to resurface on the next request).
const __chatDataDeletes = new Set();
const __userDataDeletes = new Set();
const __cantripDataDeletes = new Set();

context.chat_data = {
    get: (key) => key in __chatData ? __chatData[key] : null,
    set: (key, value) => { __chatData[key] = value; __chatDataDeletes.delete(key); },
    keys: () => Object.keys(__chatData),
    delete: (key) => { delete __chatData[key]; __chatDataDeletes.add(key); }
};

context.user_data = {
    get: (key) => key in __userData ? __userData[key] : null,
    set: (key, value) => { __userData[key] = value; __userDataDeletes.delete(key); },
    keys: () => Object.keys(__userData),
    delete: (key) => { delete __userData[key]; __userDataDeletes.add(key); }
};

context.cantrip_data = {
    get: (key) => key in __cantripData ? __cantripData[key] : null,
    set: (key, value) => { __cantripData[key] = value; __cantripDataDeletes.delete(key); },
    keys: () => Object.keys(__cantripData),
    delete: (key) => { delete __cantripData[key]; __cantripDataDeletes.add(key); }
};

const __memories = context.__memories || {};
delete context.__memories;
const __memoryDeletes = new Set();
context.memory = {
    get: (key) => key in __memories ? __memories[key] : null,
    set: (key, value) => { __memories[key] = String(value); __memoryDeletes.delete(key); },
    keys: () => Object.keys(__memories),
    delete: (key) => { delete __memories[key]; __memoryDeletes.add(key); },
    all: () => Object.assign({}, __memories)
};

// Map nominations (0.33.0): a script can ask for a map by tag. Collection
// only -- the host treats it as a selection candidate under the Activation
// Hierarchy, never a guaranteed run.
const __mapNominations = [];
context.maps = {
    activate: (tag) => {
        if (typeof tag === 'string' && tag) __mapNominations.push(tag);
    }
};

// Package 3 (items 8 + 4): prompt-side requests collected for the host's
// post-cantrip application pass. activate(name) turns one of the user's
// existing static lorebooks on for THIS request (union source: adds, never
// subtracts); inject(text, position) places text in the prompt. Neither
// persists -- re-running the script re-requests them.
const __bookActivations = [];
const __injections = [];
context.activate = (name) => {
    if (typeof name === 'string' && name.trim()) {
        __bookActivations.push(name.trim().slice(0, 128));
    }
};
context.inject = (text, position) => {
    if (typeof text !== 'string' || !text.trim()) return;
    if (__injections.length >= 20) {
        __debugLogs.push('inject: refused, per-request cap (20) reached');
        return;
    }
    const pos = ['system_start', 'before_last_message', 'in_chat_system'].indexOf(position) !== -1
        ? position : 'before_last_message';
    __injections.push({ text: text, position: pos });
};

// Pipeline ordering (0.41.0): strip(pattern, scope) asks the host to remove
// regex matches from the prompt's message text. scope: 'all' (default),
// 'system' (system-role messages only), 'messages' (non-system). Applied
// host-side at this cantrip's step in manual order, or right after the
// pre-driver pass otherwise -- later lorebook slots match the cleaned text.
// Collection only; a pattern that does not compile degrades to
// skipped-with-log on the host, never a failed request.
const __promptStrips = [];
context.strip = (pattern, scope) => {
    if (typeof pattern !== 'string' || !pattern.trim()) return;
    if (__promptStrips.length >= 20) {
        __debugLogs.push('strip: refused, per-request cap (20) reached');
        return;
    }
    const sc = ['all', 'system', 'messages'].indexOf(scope) !== -1 ? scope : 'all';
    __promptStrips.push({ pattern: pattern.slice(0, 256), scope: sc });
};

// Dynamic lorebooks (Phase 20): reads are served from a host-seeded copy
// (this conversation's script-created books always; static books named in the
// cantrip's lorebook_reads config); writes are collected as an op log the
// host applies with guards after the run. No host callbacks -- the sandbox
// stays deny-all. Books seeded with writable=false refuse writes; create is
// get-or-create so a script that runs every turn reuses its book instead of
// duplicating it.
const __lorebookSeed = context.__lorebook || {};
delete context.__lorebook;
const __lorebookOps = [];
const __lorebookBooks = {};
for (const __bname of Object.keys(__lorebookSeed)) {
    const __b = __lorebookSeed[__bname] || {};
    __lorebookBooks[__bname] = {
        writable: __b.writable === true,
        entries: (__b.entries || []).map(e => Object.assign({}, e))
    };
}

function __lorebookEntryKeys(arr) {
    if (!Array.isArray(arr)) return null;
    return arr.map(k => String(k).trim()).filter(k => k).slice(0, 32);
}

function __normLorebookEntry(entry) {
    if (typeof entry !== 'object' || entry === null) return null;
    const content = typeof entry.content === 'string' ? entry.content : '';
    if (!content.trim()) return null;
    const keys = __lorebookEntryKeys(entry.keys) || [];
    const secondary = __lorebookEntryKeys(entry.secondary_keys) || [];
    return {
        name: (typeof entry.name === 'string' ? entry.name : '').slice(0, 128),
        keys,
        secondary_keys: secondary,
        content,
        position: typeof entry.position === 'string' ? entry.position : 'before_last_message',
        insertion_order: Number.isFinite(entry.insertion_order) ? Math.trunc(entry.insertion_order) : 10,
        is_constant: entry.is_constant === true,
        is_selective: entry.is_selective === true,
        character_limit: Number.isFinite(entry.character_limit)
            ? Math.max(0, Math.trunc(entry.character_limit)) : 0
    };
}

function __lorebookOpAllowed() {
    // Local half of the op cap; the host's per-request counter is
    // authoritative (it spans every cantrip position and map stage).
    if (__lorebookOps.length >= 50) {
        __debugLogs.push('lorebook: op refused, per-run op cap (50) reached');
        return false;
    }
    return true;
}

function __findLorebookEntry(book, entryName, allowKeyFallback = true) {
    // 0.42.0 (B8b): name matching is case-sensitive, mirroring the host's
    // _find_entry -- add({name: "tavern"}) must not silently overwrite an
    // entry named "Tavern". The keys fallback still folds case and is only
    // used by update/delete, never add().
    const name = String(entryName);
    let hit = book.entries.find(e => (e.name || '') === name);
    if (hit || !allowKeyFallback) return hit || null;
    // Exact-key fallback: an entry known by a keyword rather than its name.
    const lower = name.toLowerCase();
    return book.entries.find(e => (e.keys || []).some(k => String(k).toLowerCase() === lower));
}

function __wrapLorebook(name) {
    const book = __lorebookBooks[name];
    const requireWritable = (op) => {
        if (!book.writable) {
            __debugLogs.push('lorebook: ' + op + ' refused, book is read-only: ' + name);
            return false;
        }
        return __lorebookOpAllowed();
    };
    return {
        add: (entry) => {
            const norm = __normLorebookEntry(entry);
            if (!norm) {
                __debugLogs.push('lorebook: add refused, entry needs content');
                return;
            }
            if (!requireWritable('add')) return;
            __lorebookOps.push({ op: 'add', book: name, entry: norm });
            // The host upserts by exact name only (never key-fallback on add).
            const existing = norm.name ? __findLorebookEntry(book, norm.name, false) : null;
            if (existing) Object.assign(existing, norm);
            else book.entries.push(norm);
        },
        update: (entryName, fields) => {
            if (typeof entryName !== 'string' || !entryName.trim()) {
                __debugLogs.push('lorebook: update refused, entry name required');
                return;
            }
            if (typeof fields !== 'object' || fields === null) {
                __debugLogs.push('lorebook: update refused, fields object required');
                return;
            }
            if (!requireWritable('update')) return;
            const target = __findLorebookEntry(book, entryName);
            if (!target) {
                __debugLogs.push('lorebook: update refused, no entry "' + entryName + '" in ' + name);
                return;
            }
            __lorebookOps.push({ op: 'update', book: name, entry: entryName, fields: fields });
            if (typeof fields.content === 'string') target.content = fields.content;
            if (typeof fields.name === 'string') target.name = fields.name.slice(0, 128);
            const fk = __lorebookEntryKeys(fields.keys);
            if (fk) target.keys = fk;
            const fsk = __lorebookEntryKeys(fields.secondary_keys);
            if (fsk) target.secondary_keys = fsk;
            if (typeof fields.position === 'string') target.position = fields.position;
            if (Number.isFinite(fields.insertion_order)) target.insertion_order = Math.trunc(fields.insertion_order);
            if (typeof fields.is_constant === 'boolean') target.is_constant = fields.is_constant;
            if (typeof fields.is_selective === 'boolean') target.is_selective = fields.is_selective;
            if (Number.isFinite(fields.character_limit)) target.character_limit = Math.max(0, Math.trunc(fields.character_limit));
        },
        delete: (entryName) => {
            if (typeof entryName !== 'string' || !entryName.trim()) return;
            if (!requireWritable('delete')) return;
            const target = __findLorebookEntry(book, entryName);
            if (!target) {
                __debugLogs.push('lorebook: delete refused, no entry "' + entryName + '" in ' + name);
                return;
            }
            __lorebookOps.push({ op: 'delete', book: name, entry: entryName });
            book.entries.splice(book.entries.indexOf(target), 1);
        },
        entries: () => book.entries.map(e => Object.assign({}, e)),
        search: (query) => {
            const words = String(query || '').toLowerCase().split(/\s+/).filter(w => w);
            if (!words.length) return [];
            return book.entries.filter(e => {
                const hay = ((e.name || '') + ' ' + (e.keys || []).join(' ')
                    + ' ' + (e.content || '')).toLowerCase();
                return words.some(w => hay.includes(w));
            }).map(e => Object.assign({}, e));
        }
    };
}

context.lorebook = {
    create: (cfg) => {
        const name = typeof cfg === 'object' && cfg !== null && typeof cfg.name === 'string'
            ? cfg.name.trim().slice(0, 128) : '';
        if (!name) {
            __debugLogs.push('lorebook: create refused, name required');
            return null;
        }
        if (__lorebookBooks[name]) return __wrapLorebook(name);
        if (!__lorebookOpAllowed()) return null;
        __lorebookBooks[name] = { writable: true, entries: [] };
        __lorebookOps.push({
            op: 'create', book: name,
            description: typeof cfg.description === 'string' ? cfg.description.slice(0, 2000) : '',
            tag: typeof cfg.tag === 'string' ? cfg.tag.trim().slice(0, 128) : ''
        });
        return __wrapLorebook(name);
    },
    get: (name) => {
        return typeof name === 'string' && __lorebookBooks[name] ? __wrapLorebook(name) : null;
    }
};

// Passenger (Phase 17): the sanctioned LLM egress. The host injects either
// null (feature off or no handler: enabled=false, call() resolves null with
// zero protocol traffic) or {"enabled":true,"maxCalls":N}. Requests go out
// one JSON line per call on stderr behind the __GITV_MSG__ sentinel (so Deno
// diagnostics can never be mistaken for protocol traffic); replies arrive as
// JSON lines on stdin, matched by id. The host-side budget counter is
// authoritative -- the local one only answers over-cap calls without a round
// trip.
const __passengerCfg = __GITV_PASSENGER__;
const __passengerPending = new Map();
let __passengerSeq = 0;
let __passengerLocalUsed = 0;
const __GITV_MSG_PREFIX = "__GITV_MSG__ ";

function __passengerCall(params) {
    return new Promise((resolve) => {
        if (!__passengerCfg || !__passengerCfg.enabled) {
            __debugLogs.push('passenger: call refused, passenger is disabled');
            resolve(null);
            return;
        }
        if (typeof params !== 'object' || params === null) {
            __debugLogs.push('passenger: call refused, params must be an object');
            resolve(null);
            return;
        }
        const tag = typeof params.tag === 'string' ? params.tag : '';
        const instructions = typeof params.instructions === 'string' ? params.instructions : '';
        const ctx = typeof params.context === 'string' ? params.context : '';
        const model = typeof params.model === 'string' ? params.model : '';
        if (!instructions.trim()) {
            __debugLogs.push('passenger: call refused, instructions must be a non-empty string');
            resolve(null);
            return;
        }
        if (__passengerLocalUsed >= __passengerCfg.maxCalls) {
            __debugLogs.push('passenger: call refused, per-request call cap reached');
            resolve(null);
            return;
        }
        __passengerLocalUsed += 1;
        __passengerSeq += 1;
        const id = __passengerSeq;
        __passengerPending.set(id, resolve);
        try {
            const line = JSON.stringify({
                type: 'passenger_call', id, tag, instructions, context: ctx, model
            });
            Deno.stderr.writeSync(new TextEncoder().encode(__GITV_MSG_PREFIX + line + "\n"));
        } catch (e) {
            __passengerPending.delete(id);
            __debugLogs.push('passenger: call failed, ' + (e.message || String(e)));
            resolve(null);
        }
    });
}

// Started before the user code so replies can arrive while it runs. ONLY
// when Passenger is enabled: an open stdin read keeps Deno's event loop
// alive, so an unconditional reader would hang every plain cantrip run to
// its timeout on the non-interactive path where stdin is inherited. On
// stdin EOF (process exiting) every still-pending call resolves null, so
// the final result can always serialize. Unknown ids and malformed lines
// are ignored with a debug-log note, never thrown.
let __passengerStdinLoop = null;
if (__passengerCfg && __passengerCfg.enabled) {
    __passengerStdinLoop = (async () => {
        const decoder = new TextDecoder();
        let buffer = "";
        try {
            for await (const chunk of Deno.stdin.readable) {
                buffer += decoder.decode(chunk, { stream: true });
                let idx;
                while ((idx = buffer.indexOf("\n")) !== -1) {
                    const line = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 1);
                    if (!line.trim()) continue;
                    let msg = null;
                    try { msg = JSON.parse(line); } catch { /* noted below */ }
                    if (msg && msg.type === 'passenger_result' && typeof msg.id === 'number') {
                        const resolveFn = __passengerPending.get(msg.id);
                        if (resolveFn) {
                            __passengerPending.delete(msg.id);
                            resolveFn(msg.ok === true && typeof msg.text === 'string' ? msg.text : null);
                        } else {
                            __debugLogs.push('passenger: stdin reply for unknown id ' + msg.id);
                        }
                    } else {
                        __debugLogs.push('passenger: ignored malformed stdin line');
                    }
                }
            }
        } catch { /* stdin closed underneath us; resolve pending below */ }
        for (const resolveFn of __passengerPending.values()) resolveFn(null);
        __passengerPending.clear();
    })();
}

context.passenger = {
    enabled: !!(__passengerCfg && __passengerCfg.enabled),
    call: __passengerCall
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

if (typeof context.tool_result !== 'string') context.tool_result = "";

let __error = null;
try {
    // The async wrapper makes top-level `await` work (Passenger calls are
    // Promises); purely synchronous scripts are unaffected. The trim fallback
    // tolerates user code ending in a stray closing brace, as before.
    let fn;
    try {
        fn = new Function('context', 'return (async () => {\n' + __userCode + '\n})();');
    } catch (e1) {
        const trimmed = __userCode.replace(/\n\s*\}\s*$/, '');
        fn = new Function('context', 'return (async () => {\n' + trimmed + '\n})();');
    }
    await fn(context);
} catch (e) {
    __error = e.message || String(e);
    __debugLogs.push('SCRIPT ERROR: ' + __error);
}

// User code done: close stdin so the reply reader EOFs and the process can
// exit -- an open stdin read keeps Deno's event loop alive. Calls the script
// never awaited resolve null (their replies cannot arrive after this); a
// script that wants a result must await it.
if (__passengerStdinLoop) {
    try { Deno.stdin.close(); } catch { /* already closed */ }
    try { await __passengerStdinLoop; } catch { /* reader aborted */ }
}

const __result = {
    personality: context.character.personality || "",
    scenario: context.character.scenario || "",
    example_dialogs: context.character.example_dialogs || "",
    response_content: __hasResponse ? (context.response.content !== undefined && context.response.content !== null ? context.response.content : null) : null,
    tool_result: context.tool_result || "",
    chat_data: Object.assign({}, __chatData),
    user_data: Object.assign({}, __userData),
    cantrip_data: Object.assign({}, __cantripData),
    chat_data_deletes: Array.from(__chatDataDeletes),
    user_data_deletes: Array.from(__userDataDeletes),
    cantrip_data_deletes: Array.from(__cantripDataDeletes),
    memories: Object.assign({}, __memories),
    memory_deletes: Array.from(__memoryDeletes),
    maps_nominated: __mapNominations,
    lorebook_ops: __lorebookOps,
    book_activations: __bookActivations,
    injections: __injections,
    prompt_strips: __promptStrips,
    debug_logs: __debugLogs,
    error: __error
};

Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify(__result)));
