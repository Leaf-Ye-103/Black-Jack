# Blackjack

A local blackjack game for 1–7 human players sharing a browser or terminal against a casino dealer, plus a reinforcement-learning trainer. Uses play money.

`blackjack.py` is the shared game file: it contains the rules engine, terminal game, and browser server. `train_blackjack.py` imports this same engine for training and evaluation. Keep the `web/` directory beside `blackjack.py`; it supplies the browser's HTML, styles, and scripts.

## Play

Requires Python 3.10 or newer; no packages or services are needed.

### Browser table

```sh
python3 blackjack.py web
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. Set up 1–7 local players with names and starting cash, then choose the table's minimum, increment, and maximum bets. The defaults are $5, $1, and $500. Players share the same browser and take turns using chip controls and the available hand actions: hit, stand, double, split, surrender, and insurance. The dealer's hole card stays hidden until the rules require its reveal.

The server keeps the active game in memory until it stops. Browser local storage saves table setup and balances from settled rounds. Explicitly restoring these starts a table with a fresh shoe; it does not resume a saved hand or the previous shoe.

Change the listening address or port when needed:

```sh
python3 blackjack.py web --host 127.0.0.1 --port 8080
```

The browser layout takes design guidance from [vegas-games-extension](https://github.com/alpharomercoma/vegas-games-extension). Its interface and artwork are original; no source or artwork was copied. That reference uses a dealer-hits-soft-17 rule; this game's dealer continues to **stand on soft 17**, as specified below.

### Terminal game

```sh
python3 blackjack.py play
```

`python3 blackjack.py` also starts the terminal game for compatibility.

Enter the number of players, their names, and each player's starting cash. Each round, enter a bet or `0` to sit out. Actions are typed in full when prompted. Choose whether to continue after each round.

The default table has a **$5 minimum, $1 bet increments, and a $500 maximum**. Set these when starting the game, for example:

```sh
python3 blackjack.py play --min-bet 3 --bet-increment 0.5 --max-bet 9999
```

Opening bets must be the minimum plus a whole number of increments, within the table maximum and your available cash. This example allows $3, $3.50, $4, and so on. Entering `0` always sits out. If the maximum falls between allowed amounts, the highest allowed amount below it is the effective ceiling. A player with less than the minimum cannot bet, including an all-in bet below the minimum.

## Table rules

Vegas and Macau casinos offer different tables. This game uses this explicit rule set:

- Six shuffled decks shared by all players; shuffle between rounds when fewer than half the cards remain.
- Dealer stands on all 17s, including soft 17. Aces count as 1 or 11.
- Dealer takes a hole card and checks for blackjack before player actions.
- Natural blackjack pays 3:2; ordinary wins pay 1:1; ties return the bet.
- Double on any first two cards, including after splitting: add the original bet, take one card, and stand.
- Split identical ranks into up to four hands, subject to available cash. Ten and king are different ranks for splitting.
- Split aces get one card each, with no resplitting. A split-hand 21 pays 1:1.
- Late surrender on the original first two cards returns half the bet, after dealer blackjack has been ruled out.
- Insurance when the dealer shows an ace costs up to half the original bet and pays 2:1 if the dealer has blackjack.
- The minimum and increment must be positive multiples of $0.02, keeping blackjack and surrender payouts exact to cents; whole-dollar and half-dollar settings are supported. No credit or borrowing.
- The table maximum applies to each opening bet. Doubling and splitting can increase the total amount at risk beyond that maximum, subject to available cash. Insurance is a separate wager of up to half the original bet, in cents.

All players act against the same dealer; player count changes the shared card flow, not the payouts. The interactive game uses human players; the trainer below controls simulated players. There are no side bets, real-money transactions, or saved accounts.

## Learn a strategy

`train_blackjack.py` learns from simulated games without labeled examples. This is reinforcement learning: first-visit Monte Carlo control estimates action values from the final bankroll of completed sessions. Its objective is to maximize **expected final bankroll over a fixed number of rounds**, given starting funds. It does not guarantee the best strategy or profitable play.

The trainer calls the shared `blackjack.py` engine through a decision callback, so dealing, legal actions, cash accounting, and payouts use the same rules as the browser and terminal games. Existing saved models remain supported. Browser setup is independent of a model's saved training settings; use matching settings when comparing play and evaluation. Each training episode starts a fresh shoe and resets every player's bankroll. `--players` counts everyone at the table (1–7): the learner occupies seat 1, and the remaining seats use simple bots that target a one-unit bet within the table limits, decline insurance, and hit until 17. Bots' funds are not replenished during a session.

The learner chooses among these actions:

- Sit out, bet the minimum, bet the largest affordable allowed amount, or target one, two, or five units. Unit-based targets are adjusted to the allowed bet grid and capped by available cash and the table maximum; duplicate amounts are combined. This is a small menu of choices, not every possible bet.
- Decline insurance or buy the full affordable insurance amount.
- Choose any currently legal playing action, including double, split, and surrender.

Its state uses public information: the hand, dealer upcard, legal actions, bankroll and rounds remaining, and a Hi-Lo count based on exposed cards. It cannot see the dealer's hidden card or future shoe order. Bankroll, remaining rounds, and count are grouped into finite bins. Many sessions may be needed to cover these states; unseen states fall back to sitting out, declining insurance, or hitting until 17.

Train and evaluate a model with $1,000 starting cash, $10 units, 50 rounds per session, three total players, and the default $5/$1/$500 table limits:

```sh
python3 train_blackjack.py train --bankroll 1000 --unit-bet 10 --rounds 50 --players 3 --episodes 10000 --eval-sessions 1000
```

Use the same flags to train for a different table:

```sh
python3 train_blackjack.py train --bankroll 1000 --unit-bet 10 --min-bet 3 --bet-increment 0.5 --max-bet 9999 --episodes 10000
```

`--bankroll` accepts cents and must cover at least the table minimum to train; `--unit-bet` is a target amount and must be a positive multiple of $0.02. `--rounds` and `--episodes` must be positive integers. These settings, including the table limits, are saved with the model and reused for evaluation and play. Train again to learn for different table limits, bankroll, horizon, unit size, or player count. No additional Python packages are required.

For compatibility, the Python API defaults and older model files without table-limit settings retain the original $0.02 minimum, $0.02 increments, and no table maximum. The command-line defaults for new games and training runs are $5/$1/$500. Retrain an older model to use new limits.

By default, training saves `models/blackjack_model.json` and evaluates without learning on separately seeded sessions, writing `reports/blackjack_evaluation.json`. Use `--model`, `--report`, and `--seed` to select output paths and reproducible random seeds. Evaluate the saved model again or watch it play:

```sh
python3 train_blackjack.py evaluate --model models/blackjack_model.json --sessions 1000 --seed 12345
python3 train_blackjack.py play --model models/blackjack_model.json --seed 12345
```

Evaluation compares the learned policy with flat betting and hitting to 17, and with always sitting out. The report includes mean and median final bankroll, mean profit with an approximate 95% confidence interval, profitable and losing session rates, ruin rate, rounds bet, unseen-state frequency, and paired differences from the baselines. Ruin means having less cash than the table minimum, even if some funds remain. Sitting out preserves starting funds and is a meaningful baseline when participation is optional. Finite training, state grouping, and sampling uncertainty can leave a learned strategy worse than either baseline; use held-out results and their uncertainty when judging it.

## Validation

```sh
python3 -m unittest discover -s tests -v
```
