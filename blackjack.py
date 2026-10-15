"""Local multiplayer casino blackjack. Run with Python 3.10+."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import random

D = Decimal
RANKS = ('A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K')


def total(cards):
    value = sum(11 if c == 'A' else 10 if c in ('J', 'Q', 'K') else int(c) for c in cards)
    aces = cards.count('A')
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def natural(cards):
    return len(cards) == 2 and total(cards) == 21


def money(value):
    return f'${value:,.2f}'


def amount(prompt, maximum=None, minimum=D('0')):
    while True:
        try:
            value = D(input(prompt).strip())
            if not value.is_finite() or value != value.quantize(D('.01')):
                raise ValueError
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError
            return value
        except (InvalidOperation, ValueError):
            print('Enter a valid amount in cents within the available balance.')


@dataclass
class Player:
    name: str
    cash: Decimal


@dataclass
class Hand:
    cards: list[str]
    bet: Decimal
    split: bool = False
    surrendered: bool = False


class Shoe:
    def __init__(self):
        self.cards = []
        self.shuffle()

    def shuffle(self):
        self.cards = list(RANKS) * 24  # Six decks, four suits per deck.
        random.shuffle(self.cards)

    def draw(self):
        return self.cards.pop()


def settle(player, hand, dealer):
    """Return the outcome and credit returns; wager was deducted when placed."""
    if hand.surrendered:
        player.cash += hand.bet / 2
        return 'surrender (half bet returned)'
    if total(hand.cards) > 21:
        return 'bust'
    if natural(dealer):
        if natural(hand.cards) and not hand.split:
            player.cash += hand.bet
            return 'push'
        return 'dealer blackjack: loss'
    if natural(hand.cards) and not hand.split:
        player.cash += hand.bet * D('2.5')
        return 'blackjack! pays 3:2'
    if total(dealer) > 21 or total(hand.cards) > total(dealer):
        player.cash += hand.bet * 2
        return 'win'
    if total(hand.cards) == total(dealer):
        player.cash += hand.bet
        return 'push'
    return 'loss'


def play_hand(player, hands, index, shoe):
    hand = hands[index]
    if not hand.split and natural(hand.cards):
        return
    while total(hand.cards) < 21:
        choices = ['hit', 'stand']
        if len(hand.cards) == 2 and player.cash >= hand.bet:
            choices.append('double')
            if hand.cards[0] == hand.cards[1] and len(hands) < 4:
                choices.append('split')
        if not hand.split and len(hand.cards) == 2:
            choices.append('surrender')
        print(f'{player.name}, hand {index + 1}: {hand.cards} = {total(hand.cards)}; bet {money(hand.bet)}')
        action = input(f"Choose {' / '.join(choices)}: ").strip().lower()
        if action not in choices:
            print('Choose one of the displayed actions.')
            continue
        if action == 'stand':
            return
        if action == 'surrender':
            hand.surrendered = True
            return
        if action == 'double':
            player.cash -= hand.bet
            hand.bet *= 2
            hand.cards.append(shoe.draw())
            return
        if action == 'split':
            player.cash -= hand.bet
            rank = hand.cards[0]
            hand.cards = [rank, shoe.draw()]
            hand.split = True
            new_hand = Hand([rank, shoe.draw()], hand.bet, split=True)
            hands.insert(index + 1, new_hand)
            if rank == 'A':
                # Split aces receive exactly one card; no resplitting.
                return
            continue
        hand.cards.append(shoe.draw())


def round_game(players, shoe):
    # A conservative cut card leaves enough for seven players with four hands.
    if len(shoe.cards) < 156:
        shoe.shuffle()
        print('Shuffling the six-deck shoe.')
    active = []
    for player in players:
        if player.cash < D('.02'):
            print(f'{player.name} has insufficient funds to bet.')
            continue
        print(f'{player.name}: balance {money(player.cash)}')
        bet = amount('Bet (0 to sit out; multiples of $0.02): ', player.cash)
        while bet % D('.02'):
            print('Use multiples of $0.02 so 3:2 payouts and surrender stay in cents.')
            bet = amount('Bet (0 to sit out): ', player.cash)
        if bet:
            player.cash -= bet
            active.append((player, [Hand([], bet)]))
    if not active:
        print('Everyone sat out. No cards dealt.')
        return
    dealer = []
    for _ in range(2):
        for _, hands in active:
            hands[0].cards.append(shoe.draw())
        dealer.append(shoe.draw())
    print(f'Dealer shows: {dealer[0]}')
    insurance = []
    if dealer[0] == 'A':
        for player, hands in active:
            print(f'{player.name}: {hands[0].cards} = {total(hands[0].cards)}')
            cap = min(player.cash, hands[0].bet / 2)
            stake = amount(f'Insurance (0 to decline, up to {money(cap)}): ', cap)
            player.cash -= stake
            insurance.append((player, stake))
    dealer_blackjack = natural(dealer)
    for player, stake in insurance:
        if dealer_blackjack:
            player.cash += stake * 3
        if stake:
            print(f'{player.name}: insurance {"wins 2:1" if dealer_blackjack else "loses"}.')
    if not dealer_blackjack:
        for player, hands in active:
            index = 0
            while index < len(hands):
                hand = hands[index]
                if not (hand.split and hand.cards[0] == 'A'):
                    play_hand(player, hands, index, shoe)
                index += 1
        needs_dealer = any(not h.surrendered and total(h.cards) <= 21
                           and not (natural(h.cards) and not h.split)
                           for _, hands in active for h in hands)
        if needs_dealer:
            while total(dealer) < 17:
                dealer.append(shoe.draw())
    print(f'Dealer: {dealer} = {total(dealer)}')
    for player, hands in active:
        for i, hand in enumerate(hands, 1):
            outcome = settle(player, hand, dealer)
            print(f'{player.name}, hand {i}: {hand.cards} ({total(hand.cards)}) — {outcome}')
        print(f'{player.name}: balance {money(player.cash)}')


def main():
    print('BLACKJACK — local multiplayer, 1–7 players vs dealer')
    print('Six decks | blackjack 3:2 | stand soft 17 | late surrender | insurance')
    while True:
        try:
            count = int(input('Number of players (1–7): '))
            if 1 <= count <= 7:
                break
        except ValueError:
            pass
        print('Enter a whole number from 1 to 7.')
    players = []
    for i in range(count):
        name = input(f'Player {i + 1} name: ').strip() or f'Player {i + 1}'
        players.append(Player(name, amount('Starting cash: ', minimum=D('.02'))))
    shoe = Shoe()
    while True:
        round_game(players, shoe)
        if not any(p.cash >= D('.02') for p in players):
            print('No players have funds left to bet. Game over.')
            break
        if input('Play another round? [y/N]: ').strip().lower() not in ('y', 'yes'):
            break
    print('Final balances:')
    for player in players:
        print(f'{player.name}: {money(player.cash)}')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\nGame closed.')
