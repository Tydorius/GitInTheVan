// Personality Manager — Assigns random personalities to LLM players
// Pre-Driver cantrip. Runs before each LLM stage to inject personality.
// Also tracks whose "turn" it is and injects the right personality.

const PERSONALITIES = [
    { name: 'The Gambler', traits: 'reckless, loves high stakes, bluffs constantly, speaks in gambling metaphors, overconfident' },
    { name: 'The Calculator', traits: 'cold and analytical, counts cards openly, never bluffs, speaks in probabilities, gets frustrated by luck' },
    { name: 'The Showman', traits: 'theatrical, dramatic reactions to every card, narrates their thoughts aloud, loves the spotlight, prone to monologues' },
    { name: 'The Drunk', traits: 'slurred speech patterns, inappropriate bets, forgets the rules frequently, tells rambling stories between hands, surprisingly lucky' },
    { name: 'The Grifter', traits: 'smooth talker, always trying to cheat or angle-shoot, reads other players intensely, compliments opponents before screwing them over' },
    { name: 'The Amateur', traits: 'nervous, asks obvious questions, celebrates small wins too much, folds too early, genuinely surprised by basic rules' },
    { name: 'The Veteran', traits: 'seen it all, world-weary, gives unsolicited advice, plays conservatively, complains about "kids these days"' },
    { name: 'The Cheat', traits: 'tries to peek at other cards, suggests side bets, distracts the dealer, uses sleight of hand, denies everything' },
];

function getPersonalities() {
    if (!context.chat_data.get('llm_personalities')) {
        const shuffled = [...PERSONALITIES].sort(() => Math.random() - 0.5);
        const assignment = {};
        const playerKeys = ['player1', 'player2', 'player3'];
        for (let i = 0; i < playerKeys.length && i < shuffled.length; i++) {
            assignment[playerKeys[i]] = shuffled[i];
        }
        context.chat_data.set('llm_personalities', assignment);
    }
    return context.chat_data.get('llm_personalities');
}

const personalities = getPersonalities();
const gameType = context.chat_data.get('game_type') || 'blackjack';
const currentTurn = context.chat_data.get('current_player') || '';

let personalityBlock = `\n\n[GAME STATE]\n`;
personalityBlock += `Current game: ${gameType.toUpperCase()}\n`;
personalityBlock += `Current turn: ${currentTurn || 'waiting to start'}\n\n`;
personalityBlock += `[YOUR PERSONALITY]\n`;
personalityBlock += `You are playing as a character with these traits: ${currentTurn && personalities[currentTurn] ? personalities[currentTurn].traits : 'observer'}\n`;
personalityBlock += `Stay in character at all times. Express your personality through your dialogue and actions.\n`;
personalityBlock += `[/YOUR PERSONALITY]\n`;

context.character.scenario += personalityBlock;

console.log('Personality manager: ' + (currentTurn ? `${currentTurn} = ${personalities[currentTurn]?.name || 'unassigned'}` : 'no current player set'));
