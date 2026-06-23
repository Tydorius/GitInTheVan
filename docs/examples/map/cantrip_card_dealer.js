// Card Dealer — Manages deck state, dealing, and shuffling
// Driver-callable tool. Invoked via <call:cards action="..." args="...">
//
// Actions:
//   shuffle               — Reset and shuffle the deck
//   deal count="N" to="playerId"  — Deal N cards to a player (face up)
//   deal_hidden count="N" to="playerId" — Deal N cards face down
//   show_hand player="playerId"  — Show a player's current hand
//   show_table            — Show all visible cards on the table
//   hand_value player="playerId" — Calculate blackjack hand value

const SUITS = ['♠', '♥', '♦', '♣'];
const RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K'];

function getDeck() {
    let deck = context.chat_data.get('deck');
    if (!deck || deck.length === 0) {
        deck = [];
        for (const suit of SUITS) {
            for (const rank of RANKS) {
                deck.push({ rank, suit });
            }
        }
        for (let i = deck.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [deck[i], deck[j]] = [deck[j], deck[i]];
        }
        context.chat_data.set('deck', deck);
    }
    return deck;
}

function getHands() {
    if (!context.chat_data.get('hands')) {
        context.chat_data.set('hands', {});
    }
    return context.chat_data.get('hands');
}

function dealCard(toPlayer, hidden) {
    const deck = getDeck();
    if (deck.length === 0) return null;
    const card = deck.pop();
    card.hidden = hidden;
    const hands = getHands();
    if (!hands[toPlayer]) hands[toPlayer] = [];
    hands[toPlayer].push(card);
    context.chat_data.set('hands', hands);
    context.chat_data.set('deck', deck);
    return card;
}

function cardString(card) {
    return card.rank + card.suit + (card.hidden ? ' (hidden)' : '');
}

function blackjackValue(hand) {
    let value = 0;
    let aces = 0;
    for (const card of hand) {
        if (card.hidden) continue;
        if (card.rank === 'A') {
            aces++;
            value += 11;
        } else if (['J', 'Q', 'K'].includes(card.rank)) {
            value += 10;
        } else {
            value += parseInt(card.rank);
        }
    }
    while (value > 21 && aces > 0) {
        value -= 10;
        aces--;
    }
    return value;
}

function pokerRank(hand) {
    const values = hand.map(c => c.rank);
    const suits = hand.map(c => c.suit);
    const counts = {};
    for (const v of values) counts[v] = (counts[v] || 0) + 1;
    const countVals = Object.values(counts).sort((a, b) => b - a);
    const isFlush = suits.every(s => s === suits[0]);
    const sorted = values.map(v => v === 'A' ? 14 : (['J','Q','K'].includes(v) ? 10 + ['J','Q','K'].indexOf(v) + 1 : parseInt(v))).sort((a, b) => a - b);
    let isStraight = sorted.every((v, i) => i === 0 || v === sorted[i - 1] + 1);
    if (isFlush && isStraight) return 'Straight Flush';
    if (countVals[0] === 4) return 'Four of a Kind';
    if (countVals[0] === 3 && countVals[1] === 2) return 'Full House';
    if (isFlush) return 'Flush';
    if (isStraight) return 'Straight';
    if (countVals[0] === 3) return 'Three of a Kind';
    if (countVals[0] === 2 && countVals[1] === 2) return 'Two Pair';
    if (countVals[0] === 2) return 'Pair';
    return 'High Card';
}

const call = context.tool_call;
if (call && call.name === 'cards') {
    const action = call.args.action || '';
    const args = call.args;

    if (action === 'shuffle') {
        context.chat_data.set('deck', []);
        context.chat_data.set('hands', {});
        getDeck();
        context.tool_result = 'Deck shuffled. All hands cleared.';

    } else if (action === 'deal') {
        const count = parseInt(args.count) || 1;
        const to = args.to || 'unknown';
        const hidden = args.hidden === 'true';
        const dealt = [];
        for (let i = 0; i < count; i++) {
            const card = dealCard(to, hidden);
            if (card) dealt.push(cardString(card));
        }
        const hands = getHands();
        const hand = hands[to] || [];
        const gameType = context.chat_data.get('game_type') || 'blackjack';
        let extra = '';
        if (gameType === 'blackjack') {
            extra = ` (hand value: ${blackjackValue(hand)})`;
        } else if (gameType === 'poker' && hand.length >= 5) {
            extra = ` (best hand: ${pokerRank(hand)})`;
        }
        context.tool_result = `Dealt to ${to}: ${dealt.join(', ')}${extra}`;

    } else if (action === 'show_hand') {
        const player = args.player || 'unknown';
        const hands = getHands();
        const hand = hands[player] || [];
        if (hand.length === 0) {
            context.tool_result = `${player} has no cards.`;
        } else {
            const gameType = context.chat_data.get('game_type') || 'blackjack';
            let extra = '';
            if (gameType === 'blackjack') {
                extra = ` — Value: ${blackjackValue(hand)}`;
            } else if (gameType === 'poker') {
                extra = ` — Rank: ${pokerRank(hand)}`;
            }
            context.tool_result = `${player}'s hand: ${hand.map(cardString).join(', ')}${extra}`;
        }

    } else if (action === 'show_table') {
        const hands = getHands();
        let lines = [];
        for (const [player, hand] of Object.entries(hands)) {
            const visible = hand.filter(c => !c.hidden);
            const hiddenCount = hand.filter(c => c.hidden).length;
            lines.push(`${player}: ${visible.map(cardString).join(', ')}${hiddenCount > 0 ? ` + ${hiddenCount} hidden` : ''}`);
        }
        const deck = getDeck();
        context.tool_result = lines.length > 0
            ? `Table:\n${lines.join('\n')}\nDeck: ${deck.length} cards remaining`
            : 'No cards on the table.';

    } else if (action === 'hand_value') {
        const player = args.player || 'unknown';
        const hands = getHands();
        const hand = hands[player] || [];
        const gameType = context.chat_data.get('game_type') || 'blackjack';
        if (gameType === 'blackjack') {
            context.tool_result = `${player} hand value: ${blackjackValue(hand)}`;
        } else {
            context.tool_result = `${player} hand rank: ${pokerRank(hand)}`;
        }

    } else if (action === 'set_game') {
        const game = args.game || 'blackjack';
        if (game === 'blackjack' || game === 'poker') {
            context.chat_data.set('game_type', game);
            context.tool_result = `Game type set to ${game}. Deck shuffled.`;
            context.chat_data.set('deck', []);
            context.chat_data.set('hands', {});
            getDeck();
        } else {
            context.tool_result = 'Invalid game type. Use "blackjack" or "poker".';
        }

    } else {
        context.tool_result = 'Unknown action. Available: shuffle, deal, show_hand, show_table, hand_value, set_game';
    }
} else {
    const gameType = context.chat_data.get('game_type') || 'blackjack';
    context.character.scenario += `\n\n[CARD DEALER] A ${gameType} game is in progress. The deck has ${getDeck().length} cards remaining.`;
}
