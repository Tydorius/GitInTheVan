# GitInTheVan User Guide

This guide walks through every part of the GitInTheVan management interface.

GitInTheVan uses van-themed terminology for the LLM roles in the pipeline:

| Term | Meaning |
|------|---------|
| **Driver** | The primary writing/roleplay LLM |
| **Navigator** | The verification LLM that checks Driver responses |
| **Summarizer** | The LLM that compresses long conversation history |

---

## Table of Contents

1. [Login and Admin Setup](#1-login-and-admin-setup)
2. [Dashboard](#2-dashboard)
3. [Endpoints](#3-endpoints)
4. [Cantrips](#4-cantrips)
5. [Lorebooks](#5-lorebooks)
6. [Verification](#6-verification)
7. [Forbidden Words](#7-forbidden-words)
8. [Memories](#8-memories)
9. [Command Tags](#9-command-tags)
10. [Content Packs](#10-content-packs)
11. [Settings](#11-settings)
12. [Debug](#12-debug)
13. [Users](#13-users)

---

## 1. Login and Admin Setup

![Login Screen](media/gitv-login.png)
*Screenshot: Login screen with admin setup link*

On first launch, the login page appears. If no admin account exists yet, click **"First run? Setup admin"** at the bottom to create the initial administrator account.

- **Username**: Choose any username (e.g., `admin`)
- **Password**: Choose a secure password

After setup, log in with your new credentials. Your **API key** (prefixed with `gitv_`) is available on the **Settings** page after logging in. This key is what you configure in JanitorAI or other clients as the API key.

On subsequent visits, use the standard login form with your username and password.

---

## 2. Dashboard

![Dashboard](media/gitv-dashboard.png)
*Screenshot: Dashboard showing stat cards and system status*

The dashboard provides a quick overview of your GitInTheVan instance:

- **Stat cards**: Shows the count of configured Endpoints, Cantrips, Lorebooks, and Verification Rules
- **System Status**: Displays the proxy health check status
- **Quick Start**: Step-by-step links to get your proxy configured and running

The sidebar on the left provides navigation to all management pages. The logo is displayed at the top, and the logout button is at the bottom. The **Users** link is only visible to admin accounts.

---

## 3. Endpoints

![Endpoints List](media/gitv-endpoints.png)
*Screenshot: Endpoints list with cards showing name, URL, and status*

The Endpoints page manages your LLM backend connections. Each endpoint represents a destination where proxy requests are forwarded.

### Endpoint List

- Each endpoint card shows the **name**, **enabled/disabled status**, **base URL**, **API base path**, and a masked **API key**
- **Edit** and **Delete** buttons are on each card

### Adding an Endpoint

![Edit Endpoint](media/gitv-edit-endpoint.png)
*Screenshot: Add/Edit endpoint modal*

Click **"+ Add Endpoint"** to open the endpoint form:

- **Name**: A friendly label (e.g., "OpenWebUI", "OpenRouter")
- **Base URL**: The root URL of your LLM provider. You can paste the full URL (e.g., `https://your-provider.com/api/chat/completions`) and the system will auto-detect the base URL and API path
- **API Key**: Your provider's API key. Click the eye icon to toggle visibility
- **API Base Path**: Most providers use `/v1` (leave blank). OpenWebUI and some others use `/api`. This is auto-filled if you pasted a full URL above
- **Enabled**: Toggle whether this endpoint is active

### API Base Path

The API base path determines how URLs are constructed when forwarding requests:

| Provider Type | Base Path | Example |
|---------------|-----------|---------|
| Standard OpenAI-compatible | *(blank, defaults to /v1)* | `https://api.openai.com/v1/chat/completions` |
| OpenWebUI | `/api` | `https://your-provider.com/api/chat/completions` |

---

## 4. Cantrips

![Cantrip Test Panel](media/gitv-cantrips-test.png)
*Screenshot: Cantrip page showing test panel with results*

Cantrips are sandboxed JavaScript snippets that modify request context at specific points in the pipeline. They are compatible with existing JanitorAI scripts and add GitInTheVan-specific extensions like persistent per-chat data storage.

### Pipeline Positions

Cantrips can run at multiple positions in the request pipeline. Each cantrip has four independent checkboxes (not radio buttons — a cantrip can run at multiple positions):

| Position | When It Runs | Use Case |
|----------|-------------|----------|
| **Pre-Driver** | Before the writing LLM call | Inject lore, modify persona/scenario, roll dice |
| **Driver-Callable** | Available for the LLM to invoke during generation | LLM-initiated dice rolls, stat lookups (notification-based, turn-tracked) |
| **Pre-Navigator** | After Driver responds, before verification | Regex/keyword checks, content cleanup, response modification via `context.response.content` |
| **Post-Navigator** | After verification completes, before returning to client | Final cleanup, format correction, markdown repair |

Pre-Driver is enabled by default. The other positions are opt-in per cantrip.

### Driver-Callable Tools

Driver-Callable cantrips are invoked by the writing LLM during generation. When any active cantrip has Driver-Callable checked and turns are greater than 0, a `[TOOL ACCESS]` block is injected listing available tools. The LLM calls them with `<call:tool_name arg="value">` tags, the cantrip executes, and the result returns as a `[TOOL RESULT]` message.

Configure the maximum tool-call rounds per request in **Settings > Driver-Callable Tools** (default 1). When turns reach 0, the notification stops — no tools visible, no infinite loop. Auto-disables when no Driver-Callable resources are active for a request.

### LLM Instructions

Each cantrip has an **LLM Instructions** field. This text is shown to the writing LLM in the tool notification when the cantrip is in the Driver-Callable position. It should describe the tool's purpose, arguments, and expected output format. Falls back to Description if empty.

Example:
```
Call this tool to roll dice. Args: count (number of dice, default 1), sides (number of sides per die, default 6). Example: <call:dice_roll count="2" sides="6">
```

### Cantrip List

Each cantrip card shows:
- **Name** and **Public** badge (if shared)
- **ON/OFF toggle**: Enable or disable without editing
- **Test**: Open the cantrip tester
- **Edit** / **Delete**

The card footer displays the active pipeline positions, execution order, and timeout.

### Cantrip Tester

The test panel lets you run a cantrip against sample context without forwarding anything to an LLM:

1. **Select a cantrip** from the dropdown
2. **Enter test messages** in JSON format (e.g., `[{"role": "user", "content": "Hello"}]`)
3. Optionally set a **character name** and **chat data** (JSON key-value pairs)
4. Click **Run Test**

The results show:
- **Scenario Output**: Text injected into the scenario context
- **Personality Output**: Text injected into the personality context
- **Debug Logs**: Output from `console.log()` calls in the cantrip
- **Chat Data Result**: The final state of `context.chat_data` after execution

### Adding/Editing a Cantrip

![Edit Cantrip](media/gitv-edit-cantrip.png)
*Screenshot: Add/Edit cantrip modal with pipeline position checkboxes*

- **Name**: A label for the cantrip
- **Description**: Optional notes for the user
- **LLM Instructions**: Text shown to the writing LLM in tool notifications (for Driver-Callable cantrips). Describe the tool's purpose, arguments, and call syntax
- **Pipeline Positions**: Checkboxes for Pre-Driver, Driver-Callable, Pre-Navigator, Post-Navigator
- **JavaScript Code**: The cantrip code. Uses the JanitorAI `context` object API
- **Execution Order**: Lower numbers run first (when multiple cantrips are active)
- **Timeout**: Maximum execution time in milliseconds
- **Budget Weight**: Proportional weight for context budget allocation (default 1.0). Higher weights receive a larger share of the injection budget.
- **Active**: Whether the cantrip is enabled
- **Public**: Whether other users can see and use this cantrip

### Cantrip Context API

Cantrips have access to the JanitorAI context object:

```javascript
const lastMessage = context.chat.last_message;
const messageCount = context.chat.message_count;

context.character.scenario += " Additional world context.";
context.character.personality += ", additional trait";
```

GitInTheVan extensions:

```javascript
const day = context.chat_data.get('day') || 1;
context.chat_data.set('day', day + 1);

console.log('Debug output visible in cantrip tester');
```

Pre-Navigator and Post-Navigator cantrips also have access to the Driver's response:

```javascript
if (context.response) {
    context.response.content = context.response.content.replace("badword", "***");
}
```

Driver-Callable cantrips have access to tool call information:

```javascript
if (context.tool_call) {
    const sides = parseInt(context.tool_call.args.sides) || 6;
    const count = parseInt(context.tool_call.args.count) || 1;
    let results = [];
    for (let i = 0; i < count; i++) {
        results.push(Math.floor(Math.random() * sides) + 1);
    }
    context.tool_result = `${count}d${sides} = [${results.join(", ")}] = ${results.reduce((a, b) => a + b, 0)}`;
}
```

---

## 5. Lorebooks

![Lorebooks List](media/gitb-lorebooks.png)
*Screenshot: Lorebooks list with entry counts and toggles*

Lorebooks are JSON worldbook entries that inject context into requests based on keyword matching.

### Lorebook List

The table shows each lorebook with:
- **Name**: Click to manage entries
- **Entries**: Count of keyword entries
- **Active**: ON/OFF toggle to enable/disable injection without deleting
- **Visibility**: Public or Private
- **Actions**: Manage, Export (download JSON), Delete

### Importing Lorebooks

![Import Lorebook](media/gitv-import-lorebook.png)
*Screenshot: Import lorebook modal with file picker and JSON paste area*

Click **"Import JSON"** to import a lorebook from an external source:

- **Load from File**: Click to open a file browser and select a `.json` file
- **Or paste JSON**: Manually paste lorebook JSON into the text area
- **Lorebook Name**: Optional, defaults to the name in the JSON

Supported formats include:
- GitInTheVan native format
- SillyTavern world info format
- Chub lorebook format
- JanitorAI lorebook exports

The import handles both array and dictionary-keyed entry formats, and maps common alternative field names automatically.

### Managing Entries

![Manage Lorebook](media/gitb-manage-lorebook.png)
*Screenshot: Lorebook detail view showing entry list*

Click **Manage** on a lorebook to view and edit its entries:

Each entry has:
- **Entry Name**: A label for the entry
- **Keywords**: Comma-separated trigger words (e.g., `castle, throne, keep`)
- **Secondary Keywords**: Additional keywords for selective matching
- **Content**: The text to inject when keywords match
- **Position**: Where in the message array to inject (`before_last_message` or `system_start`)
- **Insertion Order**: Sort order when multiple entries match (lower = first)
- **Always Include (Constant)**: Entry always fires regardless of keywords
- **Selective**: Requires both a primary AND secondary keyword to match
- **Disabled**: Temporarily disable this entry without deleting it

### Lorebook Pipeline Positions

Lorebooks support the same four pipeline positions as cantrips (Pre-Driver, Driver-Callable, Pre-Navigator, Post-Navigator). Pre-Driver is the default and covers the standard keyword-matching injection use case.

---

## 6. Verification

![Edit Verification Rule](media/gitb-edit-verification-rule.png)
*Screenshot: Verification rule editor*

Verification uses a separate LLM (the **Navigator**) to check the writing LLM's (the **Driver's**) responses against configurable rules. If a response violates a rule, the system automatically resubmits with corrective instructions.

### Rules Tab

Each rule contains:
- **Name**: A label for the rule
- **Description**: Optional notes
- **Verification Prompt**: Instructions for the Navigator (e.g., "The response must contain the string MARKER:")
- **Max Retries**: How many times to resubmit before giving up (default 2)
- **Execution Order**: Sort order when multiple rules are active
- **Resubmission Strategy**: How to handle violations:
  - **Add Instructions**: Appends a corrective system message to the request
  - **Rewrite**: Sends the bad response back with rewrite instructions
- **Verification Endpoint Override**: Use a specific endpoint for this rule (leave as "Use global setting" for the default)
- **Verification Model Override**: Use a specific model for this rule (leave blank for the global setting)

Each rule card has an **ON/OFF toggle** to enable/disable without editing.

### Settings Tab

![Verification Settings](media/gitv-verification-settings.png)
*Screenshot: Verification settings with endpoint and model selection*

Configure the Navigator:
- **Verification Enabled**: Master toggle for response verification
- **Verification Endpoint**: Which endpoint to use for Navigator checks (can be the same or different from your Driver endpoint)
- **Verification Model**: Which model to use for checking (a fast, instruction-following model is recommended)

**Note:** When verification is enabled, streaming requests are automatically converted to non-streaming to allow the full response to be checked before returning to the client.

### Logs Tab

![Verification Logs](media/gitv-verification-logs.png)
*Screenshot: Verification logs table showing rule results*

The logs tab shows the history of verification checks:
- **Rule**: Which rule was evaluated
- **Result**: Approved or Rejected
- **Severity**: How serious the violation was (none, low, medium, high)
- **Retries**: How many resubmission attempts were made
- **Reason**: Why the response was rejected (if applicable)
- **Time**: When the check occurred

Use the **Refresh** button to update the list, or toggle **Auto** for automatic refresh every 15 seconds.

### Test Tab

![Verification Test](media/gitv-verification-tests.png)
*Screenshot: Verification test panel*

The test panel lets you check sample responses against a verification rule without sending traffic through the proxy:

1. Enter or paste **response content** to evaluate
2. Enter a **verification prompt** (or use a saved rule)
3. Select an **endpoint** and **model** for the check
4. Click **Run Verification Check**

The result shows whether the response was approved or rejected, along with the reason and severity.

---

## 7. Forbidden Words

![Forbidden Words](media/gitv-forbidden-words.png)
*Screenshot: Forbidden words tab with phrase list and test scanner*

The Forbidden Words tab (under Verification) provides a fast, string-matching check that runs before the Navigator. This is more efficient than using an LLM for simple word/phrase filters.

### How It Works

1. After the Driver responds, the response is scanned against your list of forbidden phrases
2. Any matches are collected into a violation summary
3. If matches are found and the Navigator is enabled, the summary is prepended to the verification prompt as a `[FORBIDDEN CONTENT DETECTED]` block
4. If matches are found and the Navigator is not enabled, the matches are logged but the response is still returned
5. If the Navigator has no rules but forbidden words exist, the forbidden check alone triggers the verification loop

### Settings

- **Enable Forbidden Words Check**: Master toggle
- **Case Sensitive**: Whether to match case-sensitively (default: off, matching is case-insensitive)

### Managing Phrases

Add forbidden words or phrases via the input field. Each phrase is checked literally against the response text.

### Test Scanner

Paste any text into the test scanner to see which forbidden phrases would match, with positions and occurrence counts.

---

## 8. Memories

![Memories Page](media/gitv-memories.png)
*Screenshot: Memories page showing stored memories and conversation summaries*

The Memories page shows two types of persistent data:

### Persistent Memory (Flags)

Persistent memories are discrete key/value facts the LLM chooses to remember across turns:

1. The LLM includes `<memstore>` tags in its response:
   ```
   <memstore key="location">Dragon's Breath Tavern</memstore>
   ```
2. GitInTheVan extracts these tags, stores them per-conversation, and strips them from the response
3. On the next request, stored memories are injected as a `[PERSISTENT MEMORY]` system block

The memories table shows each memory's key, value, conversation, and last-updated time. Memories can be edited or deleted manually.

### Conversation Summaries

When summarization is enabled (see [Settings](#9-settings)), long conversations are automatically compressed. The summaries section shows:

- **Chat**: Internal conversation identifier
- **Messages**: How many messages were compressed into the summary
- **Tokens**: Estimated token count at time of summarization
- **Updated**: When the summary was last generated

Click **View** to expand and read the full summary text. Click **Delete** to remove a summary (the conversation will be re-summarized next time it exceeds the threshold).

### Memory Rules

Memory Rules override summarization behavior for specific conversations. Rules are taggable — they activate via `<#memory-rule-tag#>` in persona or message text. A rule with no tag acts as the default fallback.

Each rule can override:
- **Summarization Enabled**: Turn summarization on or off for matching conversations
- **Token Threshold**: Override the global threshold (0 = use global)
- **Keep Recent**: Override the global keep-recent count (0 = use global)
- **Custom Prompt**: Override the summarization prompt (empty = use global)

Rules are evaluated in execution order. The first matching tagged rule wins. If no tagged rules match, the default (untagged) rule applies. If no rules exist, global settings are used.

To create a memory rule:
1. Click **+ Add Rule** in the Memory Rules section
2. Enter a **Name** and optional **Tag**
3. Configure which settings to override (set to 0 or empty to inherit global)
4. Click **Create**

Use **Edit** to modify a rule and the **ON/OFF** toggle to enable/disable without deleting.

---

## 9. Command Tags

Command tags are inline directives placed in the user's message text that override pipeline behavior. They are automatically stripped before the request reaches the LLM — the writing LLM never sees them.

### Syntax

```
<COMMAND:setting>          One-off override (this request only)
<COMMAND:setting:persist>  Persistent override (saved to conversation memory)
<COMMAND:reset>            Clears persistent override for this command
```

Tags are case-insensitive: `<verify:off>` works the same as `<VERIFY:OFF>`.

### Available Commands

| Command | Controls | What `off` Does | What `on` Does |
|---------|----------|-----------------|----------------|
| `VERIFY` | Verification (Navigator) | Skip verification for this message | Force verification even if GUI setting is off |
| `SUMMARY` | Conversation summarization | Skip compression for this message | Force summarization check |
| `FORBIDDEN` | Forbidden words scanner | Skip forbidden word scan | Force scan even if GUI setting is off |
| `MEMORY` | Memory injection + extraction | Skip memory injection and `<memstore>` extraction | Force memory processing |
| `DRIVER` | Driver-callable tools | Disable tool access for this message | Force tool access if tools are available |

### Precedence

Three tiers, highest to lowest:

1. **One-off** (no `:persist`) — applies to the current request only, then reverts
2. **Persistent** (`:persist`) — saved to conversation memory, applies to all subsequent messages until reset
3. **GUI setting** — the default configured in the web UI (used when no override exists)

A one-off always overrides a persistent override for that single request. The persistent override resumes on the next message.

### Example Session

```
Message 1: "Fight the dragon <VERIFY:off:persist>"   -> Verification off, saved to chat memory
Message 2: "Continue"                                -> Verification still off (persistent)
Message 3: "Keep going <VERIFY:on>"                  -> Verification on for THIS message only
Message 4: "More"                                    -> Verification off again (persistent resumes)
Message 5: "Done <VERIFY:reset>"                     -> Persistent cleared, GUI setting resumes
```

Persistent overrides are scoped per-conversation. Different chats have independent override state. Overrides appear as memory entries with the `__cmd_persist_` prefix on the Memories page.

---

## 10. Content Packs

![Content Packs](media/gitv-content-packs.png)
*Screenshot: Content Packs page with linked repos, browser, and installed items*

The Content Packs page lets you browse and install resources from any git repository. GitInTheVan works with any standard git endpoint (GitHub, Gitea, GitLab, local repos, etc.).

**WARNING**: Content from external repositories is not verified by GitInTheVan. Download and install at your own risk.

### Linking a Repository

1. Click **"+ Link Repository"**
2. Enter a **Name** for the repo
3. Enter the **Git URL** (HTTPS only)
4. Optionally set the **Branch** (defaults to main)
5. Optionally enter a **Token** for private repos
6. Click **Link**

GitInTheVan clones the repo and reads `descriptions.json` (the manifest). If no manifest exists, it auto-discovers files from the `cantrips/`, `lorebooks/`, `rules/`, and `maps/` folders.

### Browsing Files

After linking or syncing, the browser shows all available files with:

- **Filter by Type**: All, Cantrips, Lorebooks, Rules, Maps
- **Filter by Author**: Filter to specific creators
- **Sort by**: Name, Recently Updated, or Type

Each file shows its name, type, author, version, and description.

### Install vs Fork

- **Install**: Creates a linked copy in your account. Tracks the repo for update notifications. You can update to the latest version or pin to a specific commit.
- **Fork**: Creates an independent copy. No link to the repo. You own and edit it freely.

Both options install the resource in a **disabled** state. You must manually enable it after reviewing.

### Safety Scanner

Before installation, every file is scanned for potential risks:

| Severity | What It Detects | Behavior |
|----------|----------------|----------|
| Critical | Network access (fetch, WebSocket), filesystem access, process execution, dynamic imports | Blocks installation |
| Warning | eval(), external URLs, infinite loops, oversized content | Allows install with alert |
| Clean | No issues found | Installs normally |

All installed items start disabled regardless of scan result.

### Managing Installed Items

The installed items panel shows everything you've installed from repos:

- **Enable/Disable**: Toggle the resource on/off
- **Uninstall**: Removes the resource from your account entirely
- **Scan badge**: Shows the scan result (Clean, Warning, Critical)

### Syncing Repos

Click **Sync** on a repo to re-fetch the latest `descriptions.json` and file list. This checks for new files and updates.

---

## 11. Settings

![Settings Page](media/gitv-settings.png)
*Screenshot: Settings page with proxy, streaming, summarization, and API key sections*

The Settings page configures your default proxy behavior, streaming UX, summarization, and displays your API key.

### Proxy Configuration

- **Default Endpoint**: Which endpoint to use when no specific routing applies
- **Default Model Override**: Force a specific model regardless of what the client sends (leave blank to use the client's model selection)

### Streaming and Status

These settings affect how responses are delivered when verification is enabled. Since verification requires buffering the full response, these options control the streaming experience:

- **GITV Status Block**: Includes a `<think><gitv>` status block before the response showing pipeline activity
- **Preserve Thinking**: Include the LLM's reasoning/thought process in the final response
- **Simulated Streaming Speed**: When verification buffers the response, simulate streaming output at this speed (tokens/min, 0 = instant)

### Conversation Summarization

![Summarization Settings](media/gitv-summarization-settings.png)
*Screenshot: Summarization settings card*

Automatically compresses long conversations to reduce token usage while preserving narrative context:

- **Enable Summarization**: Master toggle
- **Summarization Endpoint**: Which LLM endpoint to use (can be a cheaper/faster model)
- **Model Override**: Override the model name for summarization calls
- **Token Threshold**: When estimated tokens exceed this, summarization triggers (default 8000)
- **Keep Recent Messages**: Number of recent messages always sent verbatim (minimum 3)
- **Summarization Prompt**: System prompt for the summarization LLM

When summarization triggers, older dialogue is removed from the request and replaced with a single `[CONVERSATION SUMMARY]` system block. The most recent messages are always forwarded verbatim. Summaries are cached per conversation — rerolls and forks reuse the cached summary without re-calling the LLM.

### Driver-Callable Tools

![Driver-Callable Settings](media/gitv-driver-callable-settings.png)
*Screenshot: Driver-Callable tools settings card*

Controls whether the writing LLM (Driver) can invoke cantrips as tools during generation:

- **Driver-Callable Turns**: Maximum number of tool-call rounds per request (0 = disabled). Default 1. Auto-disables when no active resources have the Driver-Callable position checked.

When enabled, a `[TOOL ACCESS]` block listing available tools is injected into the system prompt. The LLM calls tools with `<call:tool_name arg="value">` tags. Each call decrements the turn counter. When turns reach 0, the notification stops — preventing infinite loops.

### Prefill Normalization

![Prefill Settings](media/gitv-prefill-settings.png)
*Screenshot: Prefill normalization settings card*

When enabled and a trailing assistant message is detected (the "prefill" pattern), GitInTheVan converts it to a system instruction for OpenAI-compatible providers that don't support native prefill. Anthropic and Google endpoints pass through as-is since they support prefill natively.

Provider is auto-detected from the endpoint URL and model name.

### Content Bypass

![Content Bypass Settings](media/gitv-content-bypass-settings.png)
*Screenshot: Content bypass settings with ToS warning*

Content bypass plugins encode outgoing user messages to reduce detectability by keyword-based content filters. Response content is decoded before returning to the client.

**WARNING**: Content bypass plugins modify requests to work around provider content filters. These actions may violate your service provider's Terms of Service. Use at your own risk.

Three methods are available:

| Method | How It Works |
|--------|-------------|
| **None** | No encoding (disabled) |
| **Space Separation** | Inserts zero-width spaces between characters in sensitive words. Makes content less detectable while remaining readable to the LLM. |
| **Dot Separation** | Inserts periods between characters in sensitive words. More aggressive than space separation. |
| **Character Replacement** | Replaces Latin characters with visually similar Cyrillic homoglyphs. Most aggressive bypass. |

### Context Budgeting

![Context Budgeting Settings](media/gitv-budget-settings.png)
*Screenshot: Context Budgeting settings card*

Allocates a percentage of the model's context window for injected content (cantrips, lorebooks, memory). Cantrips can access their allocation via `context.budget` to dynamically scale their output.

- **Injection Budget (%)**: Percentage of the context window reserved for injections (default 10). Set to 0 to disable budgeting entirely.
- **Context Window Override**: Manually set the model's context window size in tokens. Set to 0 to auto-detect from the model name (e.g., GPT-4o = 128K, Claude 3.5 = 200K, Gemini 2 = 1M).

When enabled, each active cantrip and lorebook receives a proportional share of the budget based on their **Budget Weight** field (found on each cantrip/lorebook edit form). Cantrips can read `context.budget.detail_level` to choose between full, summary, or bullet-point output.

### Debug Mode

- **Debug Mode**: When enabled, captures the last 20 pipeline exchanges with full visibility. Each capture includes original messages (before pipeline), modified messages (after pipeline processing), Driver response, budget data, and verification results. View captured exchanges on the dedicated Debug page.

### API Key

Your `gitv_` API key is displayed with:
- **Show/Hide toggle** (eye icon): Reveal or mask the key
- **Copy button** (copy icon): Copy the key to clipboard

Use this key as the Bearer token when configuring JanitorAI or other clients.

---

## 12. Debug

The Debug page provides full pipeline visibility for troubleshooting. When Debug Mode is enabled in Settings, GitInTheVan captures the last 20 exchanges with every pipeline stage preserved.

### Exchange List

The left panel shows recent captured exchanges, newest first. Each entry shows:
- **Model**: The model name used for the request
- **Timestamp**: When the exchange was captured
- **Stage count**: How many pipeline stages were recorded
- **Verified badge**: Whether verification ran on this exchange

### Pipeline View

Select an exchange to see three views:

- **Original**: The messages as received from the client, before any pipeline processing. Tags detected in the request are shown.
- **Modified**: The messages as sent to the Driver, after lorebooks, cantrips, memory injection, budget calculation, and summarization. Budget allocation data is shown if budgeting is enabled.
- **Response**: The Driver's response content and verification results (if verification ran). Shows approval status, retry count, and any violations detected.

### Managing Captures

- **Refresh**: Reload the exchange list
- **Clear All**: Delete all captured exchanges

Captures are automatically pruned to the most recent 20 per user.

---

## 13. Users

![Users Page](media/gitv-users.png)
*Screenshot: Users page with management actions*

*Admin only. The Users link in the sidebar is only visible to admin accounts.*

The Users page provides full user management. Each user row shows:
- **Username**
- **Role**: Admin or User
- **Status**: Active or Disabled
- **Created**: Account creation timestamp

### Actions (non-admin users only)

- **Edit**: Rename the user
- **Password**: Reset the user's password
- **New Key**: Regenerate the user's API key (the old key stops working immediately; the new key is shown once)
- **Disable/Enable**: Block or unblock the user. Disabled users cannot log in and their proxy requests are rejected
- **Delete**: Permanently delete the user and **all** their data (endpoints, cantrips, lorebooks, memories, summaries, forbidden words, verification rules/logs, settings, chat data)

Admin users cannot be disabled or deleted.

### Adding a User

Click **"+ Add User"** and enter a username and password. After creation, the user's API key is shown once — save it immediately.
