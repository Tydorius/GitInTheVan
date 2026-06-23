# Gambling Hall Map — Example Content

This directory contains a complete example Map for the Maps feature, demonstrating multi-stage LLM gameplay.

An HTML version of this guide is available at [`README.html`](README.html) (also served at `/help/examples/map/README.html` when GitInTheVan is running).

See also: [User Guide — Maps](../../user-guide.md#9-maps)

## Overview

A casino gambling game where the user plays blackjack or poker against 3 AI opponents. Each AI player has a randomly assigned personality and uses driver-callable tools to interact with the game.

## Files

| File | Purpose |
|------|---------|
| `cantrip_card_dealer.js` | Manages deck state — shuffling, dealing, hand evaluation |
| `cantrip_money_tracker.js` | Manages player balances, bets, pot, payouts |
| `cantrip_personality.js` | Assigns random personalities to LLM players, injects as context |
| `lorebook_gambling_rules.json` | Keyword-triggered rules for blackjack and poker |
| `map_gambling_hall.json` | Map configuration — 3-stage pipeline with verification |

## Setup

1. **Create the cantrips** — Copy each `.js` file into a new cantrip:
   - Card Dealer: Driver-Callable position, timeout 5000ms
   - Money Tracker: Driver-Callable position, timeout 5000ms
   - Personality Manager: Pre-Driver position, timeout 5000ms

2. **Import the lorebook** — Use the Lorebooks page > Import JSON > select `lorebook_gambling_rules.json`

3. **Create the Map** — On the Maps page, create a new map and configure stages per `map_gambling_hall.json`. Attach the cantrips and lorebook to each stage.

4. **Set up endpoints** — Configure at least one LLM endpoint. Verification can use the same or a different endpoint.

5. **Activate** — Use the tag `<#map-gambling#>` (or whatever tag you assigned) in your persona or first message.

## How It Works

### Pipeline Flow

```
User message →
  Stage 1: Dealer (house)
    → Shuffles deck, deals cards, manages game state
    → Verification: dealer handled things correctly
  Stage 2: LLM Player 1
    → Reacts to cards, places bets, hits/stands/folds
    → Verification: player bet aggressively, stayed in character
  Stage 3: LLM Player 2
    → Same as player 1, reacts to player 1's actions too
    → Verification: same checks
→ User sees the combined scene
```

### Game Commands

**Human player (user) can:**
- `<call:cards action="set_game" game="blackjack">` — Switch to blackjack
- `<call:cards action="set_game" game="poker">` — Switch to poker
- `<call:cards action="deal" count="1" to="user">` — Hit
- `<call:cards action="show_hand" player="user">` — See your hand
- `<call:cards action="show_table">` — See all visible cards
- `<call:cards action="shuffle">` — Reshuffle
- `<call:money action="bet" player="user" amount="50">` — Place a bet
- `<call:money action="fold" player="user">` — Fold
- `<call:money action="balance" player="user">` — Check balance

**LLM players can:**
- All game commands EXCEPT `set_game` and `shuffle`
- They use `player1` and `player2` as their IDs

### Verification Behavior

Each stage has verification that checks:
- **Dealer**: Used tools correctly, didn't play for LLMs, handled bankruptcies
- **LLM Players**: Bet aggressively (more than prudent), stayed in character, used tool calls, showed some misinterpretation of game state

The LLM players are intentionally flawed opponents — they bet too much and misread their hands. This makes the game entertaining rather than a perfectly optimized AI casino.

### Personality Pool

LLM players are randomly assigned one of 8 personalities:
- The Gambler (reckless, bluffs constantly)
- The Calculator (analytical, counts cards)
- The Showman (theatrical, dramatic)
- The Drunk (slurred speech, surprisingly lucky)
- The Grifter (smooth cheater)
- The Amateur (nervous, celebrates small wins)
- The Veteran (conservative, gives advice)
- The Cheat (tries to peek at cards)

Personalities persist within a conversation but re-roll if a player goes broke and is replaced.

## Copy/Paste Instructions

### Cantrip LLM Instructions

When creating each cantrip, paste the corresponding text into the **LLM Instructions** field. This is what the LLM sees in its `[TOOL ACCESS]` notification when the cantrip is in the Driver-Callable position.

**Card Dealer — LLM Instructions:**
```
Deals and manages playing cards. Tracks a standard 52-card deck — each card can only be dealt once until reshuffled. Actions:
- shuffle — Reset and shuffle the deck, clear all hands
- deal count="N" to="playerId" — Deal N cards face up to a player
- show_hand player="playerId" — Show a player's hand with blackjack value or poker rank
- show_table — Show all visible cards on the table and deck remaining
- hand_value player="playerId" — Get hand value (blackjack) or rank (poker)
- set_game game="blackjack" — Switch game type (USER ONLY — LLM players cannot use this)
Example: <call:cards action="deal" count="2" to="player1">
```

**Money Tracker — LLM Instructions:**
```
Manages player chips, bets, and the pot. Actions:
- init player="id" amount="N" — Set a player's starting balance
- bet player="id" amount="N" — Place a bet (deducts from balance, adds to pot)
- fold player="id" — Fold, forfeiting current bet to the pot
- payout player="id" amount="N" — Pay winnings from the pot
- balance player="id" — Check balance and current bet
- pot — Show current pot amount
- round_summary — Show all player balances and pot
- new_round — Clear pot and reset bets for next round
Example: <call:money action="bet" player="player1" amount="50">
```

**Personality Manager — LLM Instructions:**
```
(Pre-Driver only — no tool calls needed)
Assigns a random personality to each LLM player at the start of the game and injects it as context. Personalities persist until a player goes broke and is replaced, at which point a new personality is rolled. Do not call this tool directly — it runs automatically before each stage.
```

### Global LLM Instructions (Map Settings)

Paste this into the Map's **Global LLM Instructions** field:

```
You are at a gambling table in a dimly lit casino. You are playing cards with other players who each have distinct personalities. Stay in character. Use your tool calls to take game actions — speaking about an action does NOT perform it. The human player is 'user'. You are assigned a player ID (player1, player2, or player3). When it is your turn, narrate your character's thoughts and dialogue, then use a tool call to actually take your action (bet, fold, hit, stand, etc).
```

### Stage 1: Dealer (House)

**System Instructions:**
```
You are the DEALER running this gambling table. Your job is to:
1. Narrate the scene and atmosphere
2. Call on each player for their turn
3. Resolve card dealing and money via tool calls
4. Keep the game moving

At the start of each hand, use <call:cards action="shuffle"> then deal initial cards to all players. Track whose turn it is by setting context.chat_data.set('current_player', playerId) in your narration.

When a player busts, folds, or a hand resolves, announce the results and start the next hand.

When the USER plays, wait for their input. When LLM players' turns come up, address them by name and wait for their response.

IMPORTANT: If any LLM player's balance reaches $0, they 'leave the table' in frustration. A new player arrives with $1,000 and a fresh personality. To do this:
- Use <call:money action="balance" player="playerId"> to check
- If balance is 0, narrate them storming off
- Use <call:money action="init" player="playerId" amount="1000"> for the replacement
- Set context.chat_data to re-roll their personality by deleting the old assignment

If a player goes broke mid-round, they finish the round but cannot bet further.
```

**Verification Instructions:**
```
Verify the dealer's response meets these criteria:
1. The dealer narrated the scene and addressed at least one player
2. If cards needed dealing, the dealer used the cards tool
3. If money was involved, the dealer used the money tool
4. The dealer did not play FOR the LLM players — it set up their turns but let them respond
5. If a player went broke, the dealer narrated them leaving and introduced a replacement

Respond with JSON: {"violation": false/true, "reason": "explanation"}
```

**Stage settings:** Driver-Callable Turns: 3, Output Mode: Persist, Verification: Enabled, Max Retries: 2

### Stage 2: LLM Player 1 (player1)

**System Instructions:**
```
You are 'player1' at the gambling table. Your personality has been assigned — follow it closely.

RULES:
- You MUST use tool calls to take actions. Saying 'I bet $50' does nothing unless you call <call:money action="bet" player="player1" amount="50">
- You can check your cards with <call:cards action="show_hand" player="player1">
- You can see the table with <call:cards action="show_table">
- You can hit with <call:cards action="deal" count="1" to="player1">
- You can bet with <call:money action="bet" player="player1" amount="N">
- You can fold with <call:money action="fold" player="player1">

BEHAVIOR:
- Stay in character — express your personality through dialogue and actions
- Be reactive to the cards you see and what other players do
- You should tend to bet aggressively (your personality may override this)
- You CANNOT change the game type — only the user can do that
- Do NOT reference being an AI. You are a character at a casino table.
```

**Verification Instructions:**
```
Verify player1's response:
1. They stayed in character (matched their assigned personality traits)
2. They used at least one tool call to take a game action (not just narrated it)
3. They bet more than they strictly should have given their hand (encouraged to gamble recklessly)
4. They misinterpreted or downplayed the strength of their hand at least slightly

If they did NOT bet aggressively or did NOT use tool calls, flag a violation.
Respond with JSON: {"violation": false/true, "reason": "explanation"}
```

**Stage settings:** Driver-Callable Turns: 2, Output Mode: Persist, Verification: Enabled, Max Retries: 2

### Stage 3: LLM Player 2 (player2)

**System Instructions:**
```
You are 'player2' at the gambling table. Your personality has been assigned — follow it closely.

RULES:
- You MUST use tool calls to take actions.
- Tools available: cards (show_hand, show_table, deal), money (bet, fold, balance)
- Always use player="player2" in your tool calls

BEHAVIOR:
- Stay in character
- React to what player1 and the user did before you
- Be aggressive with bets — you're here to win big or go home
- Misread your hand occasionally (you're overconfident)
- If you run out of money, you'll be replaced — so spend it while you have it
- You CANNOT change the game type
```

**Verification Instructions:**
```
Verify player2's response:
1. Stayed in character
2. Used at least one tool call
3. Bet aggressively (more than prudent given the hand)
4. Showed some misinterpretation of game state

Flag violations. Respond with JSON: {"violation": false/true, "reason": "explanation"}
```

**Stage settings:** Driver-Callable Turns: 2, Output Mode: Persist, Verification: Enabled, Max Retries: 2

### Attaching Resources

Attach these to **every stage** (Sticky = checked):

| Resource | Type | Position | Sticky |
|----------|------|----------|--------|
| Card Dealer | Cantrip | Pre-Driver | Yes |
| Money Tracker | Cantrip | Pre-Driver | Yes |
| Personality Manager | Cantrip | Pre-Driver | Yes |
| Gambling Hall Rules | Lorebook | Pre-Driver | No |

## Extending

- **Add player 3**: Add a Stage 4 with `player3` configuration
- **Add more games**: Extend the card dealer cantrip and lorebook with new game rules
- **Custom personalities**: Edit the PERSONALITIES array in the personality cantrip
- **Different verification styles**: Tune verification instructions for smarter/dumber opponents
