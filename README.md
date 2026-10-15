# Blackjack

A local command-line blackjack game for 1–7 human players sharing a terminal against a casino dealer. Uses play money; balances last for the current session.

## Play

Requires Python 3.10 or newer; no packages or services are needed.

```sh
python3 blackjack.py
```

Enter the number of players, their names, and each player's starting cash. Each round, enter a bet or `0` to sit out. Actions are typed in full when prompted. Choose whether to continue after each round.

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
- Bets use multiples of $0.02 to keep half-bet payouts exact to cents. No credit or borrowing.

All players act against the same dealer; player count changes the shared card flow, not the payouts. The interactive game uses human players; the trainer below controls simulated players. There are no side bets, real-money transactions, or saved accounts.

## Learn a strategy

`train_blackjack.py` learns from simulated games without labeled examples. This is reinforcement learning: first-visit Monte Carlo control estimates action values from the final bankroll of completed sessions. Its objective is to maximize **expected final bankroll over a fixed number of rounds**, given starting funds. It does not guarantee the best strategy or profitable play.

The trainer calls the existing `blackjack.py` engine through a decision callback, so dealing, legal actions, cash accounting, and payouts use the same rules as the interactive game. Each training episode starts a fresh shoe and resets every player's bankroll. `--players` counts everyone at the table (1–7): the learner occupies seat 1, and the remaining seats use simple bots that bet one unit, decline insurance, and hit until 17. Bots' funds are not replenished during a session.

The learner chooses among these actions:

- Bet zero (sit out), one, two, or five units, capped at available cash and rounded to affordable even cents.
- Decline insurance or buy the full affordable insurance amount.
- Choose any currently legal playing action, including double, split, and surrender.

Its state uses public information: the hand, dealer upcard, legal actions, bankroll and rounds remaining, and a Hi-Lo count based on exposed cards. It cannot see the dealer's hidden card or future shoe order. Bankroll, remaining rounds, and count are grouped into finite bins. Many sessions may be needed to cover these states; unseen states fall back to sitting out, declining insurance, or hitting until 17.

Train and evaluate a model with $1,000 starting cash, $10 units, 50 rounds per session, and three total players:

```sh
python3 train_blackjack.py train --bankroll 1000 --unit-bet 10 --rounds 50 --players 3 --episodes 10000 --eval-sessions 1000
```

`--bankroll` accepts cents and must be at least $0.02; `--unit-bet` must be a positive multiple of $0.02. `--rounds` and `--episodes` must be positive integers. These settings are saved with the model; train again to learn for a different bankroll, horizon, unit size, or player count. No additional Python packages are required.

By default, training saves `models/blackjack_model.json` and evaluates without learning on separately seeded sessions, writing `reports/blackjack_evaluation.json`. Use `--model`, `--report`, and `--seed` to select output paths and reproducible random seeds. Evaluate the saved model again or watch it play:

```sh
python3 train_blackjack.py evaluate --model models/blackjack_model.json --sessions 1000 --seed 12345
python3 train_blackjack.py play --model models/blackjack_model.json --seed 12345
```

Evaluation compares the learned policy with flat betting and hitting to 17, and with always sitting out. The report includes mean and median final bankroll, mean profit with an approximate 95% confidence interval, profitable and losing session rates, ruin rate, rounds bet, unseen-state frequency, and paired differences from the baselines. Sitting out preserves starting funds and is a meaningful baseline when participation is optional. Finite training, state grouping, and sampling uncertainty can leave a learned strategy worse than either baseline; use held-out results and their uncertainty when judging it.

## Validation

```sh
python3 -m unittest discover -s tests -v
```
