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

All players act against the same dealer; player count changes the shared card flow, not the payouts. There are no computer-controlled players, side bets, real-money transactions, or saved accounts.

## Validation

```sh
python3 -m unittest discover -s tests -v
```
