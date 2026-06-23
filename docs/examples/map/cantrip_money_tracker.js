// Money Tracker — Manages player balances, bets, and payouts
// Driver-callable tool. Invoked via <call:money action="..." args="...">
//
// Actions:
//   init player="id" amount="N"    — Initialize a player's balance
//   bet player="id" amount="N"     — Place a bet (deducts from balance)
//   fold player="id"               — Fold, forfeiting current bet
//   payout player="id" amount="N"  — Add winnings to balance
//   balance player="id"            — Check a player's balance
//   pot                            — Show the current pot
//   round_summary                  — Show all player balances and current pot

function getBank() {
    if (!context.chat_data.get('bank')) {
        context.chat_data.set('bank', {});
        context.chat_data.set('pot', 0);
    }
    return context.chat_data.get('bank');
}

function getPot() {
    if (context.chat_data.get('pot') === undefined || context.chat_data.get('pot') === null) {
        context.chat_data.set('pot', 0);
    }
    return context.chat_data.get('pot');
}

function getBets() {
    if (!context.chat_data.get('current_bets')) {
        context.chat_data.set('current_bets', {});
    }
    return context.chat_data.get('current_bets');
}

const call = context.tool_call;
if (call && call.name === 'money') {
    const action = call.args.action || '';
    const args = call.args;

    if (action === 'init') {
        const player = args.player || 'unknown';
        const amount = parseInt(args.amount) || 1000;
        const bank = getBank();
        bank[player] = amount;
        context.chat_data.set('bank', bank);
        context.tool_result = `${player} initialized with $${amount}.`;

    } else if (action === 'bet') {
        const player = args.player || 'unknown';
        const amount = parseInt(args.amount) || 0;
        const bank = getBank();
        if (!bank[player] || bank[player] < amount) {
            context.tool_result = `${player} cannot bet $${amount} (balance: $${bank[player] || 0}).`;
        } else {
            bank[player] -= amount;
            context.chat_data.set('bank', bank);
            let pot = getPot();
            pot += amount;
            context.chat_data.set('pot', pot);
            const bets = getBets();
            bets[player] = (bets[player] || 0) + amount;
            context.chat_data.set('current_bets', bets);
            context.tool_result = `${player} bets $${amount}. Pot is now $${pot}. Balance: $${bank[player]}.`;
        }

    } else if (action === 'fold') {
        const player = args.player || 'unknown';
        const bets = getBets();
        const forfeited = bets[player] || 0;
        delete bets[player];
        context.chat_data.set('current_bets', bets);
        context.tool_result = `${player} folds. Forfeits $${forfeited} to the pot.`;

    } else if (action === 'payout') {
        const player = args.player || 'unknown';
        const amount = parseInt(args.amount) || 0;
        let pot = getPot();
        const bank = getBank();
        if (amount > pot) {
            context.tool_result = `Cannot pay $${amount} from pot ($${pot}).`;
        } else {
            pot -= amount;
            context.chat_data.set('pot', pot);
            bank[player] = (bank[player] || 0) + amount;
            context.chat_data.set('bank', bank);
            context.tool_result = `${player} wins $${amount} from the pot. Balance: $${bank[player]}. Pot remaining: $${pot}.`;
        }

    } else if (action === 'balance') {
        const player = args.player || 'unknown';
        const bank = getBank();
        const bets = getBets();
        const currentBet = bets[player] || 0;
        context.tool_result = `${player} balance: $${bank[player] || 0} (bet this round: $${currentBet}).`;

    } else if (action === 'pot') {
        context.tool_result = `Current pot: $${getPot()}.`;

    } else if (action === 'round_summary') {
        const bank = getBank();
        const bets = getBets();
        const pot = getPot();
        let lines = [`Pot: $${pot}`];
        for (const [player, balance] of Object.entries(bank)) {
            lines.push(`${player}: $${balance} (bet: $${bets[player] || 0})`);
        }
        context.tool_result = lines.join('\n');

    } else if (action === 'new_round') {
        context.chat_data.set('pot', 0);
        context.chat_data.set('current_bets', {});
        context.tool_result = 'New round started. Pot cleared. Bets reset.';

    } else {
        context.tool_result = 'Unknown action. Available: init, bet, fold, payout, balance, pot, round_summary, new_round';
    }
} else {
    const bank = getBank();
    const pot = getPot();
    if (Object.keys(bank).length > 0) {
        context.character.scenario += `\n\n[MONEY] Pot: $${pot}. Players: ${Object.entries(bank).map(([p, b]) => `${p} ($${b})`).join(', ')}.`;
    }
}
