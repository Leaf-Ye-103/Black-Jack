"""Local multiplayer casino blackjack. Run with Python 3.10+."""
import argparse
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import random
from typing import Callable

D = Decimal
RANKS = ('A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K')


@dataclass(frozen=True)
class BetLimits:
    """Legal opening bets are zero or minimum + n * increment.

    Defaults preserve the original Python API and saved models. The interactive
    and training CLIs use CASINO_LIMITS unless explicitly configured otherwise.
    Doubling, splitting, and insurance are additional wagers, not opening bets.
    """
    minimum: Decimal = D('.02')
    increment: Decimal = D('.02')
    maximum: Decimal | None = None

    def __post_init__(self):
        for name in ('minimum', 'increment', 'maximum'):
            value = getattr(self, name)
            if value is None and name == 'maximum':
                continue
            try:
                value = D(str(value))
                if not value.is_finite() or value <= 0 or value % D('.01'):
                    raise ValueError
                if name != 'maximum' and value % D('.02'):
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValueError(f'{name} must be positive cents; minimum and increment '
                                 'must be multiples of $0.02 for exact half-bet payouts.') from None
            object.__setattr__(self, name, value)
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError('Maximum bet must be at least the minimum bet.')

    def affordable_max(self, funds):
        cap = min(funds, self.maximum) if self.maximum is not None else funds
        if cap < self.minimum:
            return D(0)
        return self.minimum + ((cap - self.minimum) // self.increment) * self.increment

    def accepts(self, stake, funds):
        if (not isinstance(stake, Decimal) or not stake.is_finite()
                or stake < 0 or stake > funds or stake % D('.01')):
            return False
        return stake == 0 or (stake >= self.minimum
            and (self.maximum is None or stake <= self.maximum)
            and (stake - self.minimum) % self.increment == 0)

    def snap(self, desired, funds):
        """Fit a positive target to the affordable grid, or sit out if short."""
        cap = self.affordable_max(funds)
        if not cap or desired <= 0:
            return D(0)
        target = max(self.minimum, min(desired, cap))
        return self.minimum + ((target - self.minimum) // self.increment) * self.increment


LEGACY_LIMITS = BetLimits()
CASINO_LIMITS = BetLimits(D(5), D(1), D(500))


def add_bet_arguments(parser):
    parser.add_argument('--min-bet', default=str(CASINO_LIMITS.minimum),
                        help='Minimum opening bet (default: 5).')
    parser.add_argument('--bet-increment', default=str(CASINO_LIMITS.increment),
                        help='Opening bets are minimum + n times this increment (default: 1).')
    parser.add_argument('--max-bet', default=str(CASINO_LIMITS.maximum),
                        help='Maximum opening bet (default: 500).')


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
    policy: Callable | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Observation:
    """Public information given to an optional automated player.

    Monetary choices return Decimal amounts; play choices return an action string.
    No dealer hole card, shoe order, or mutable game objects are exposed.
    """
    phase: str
    cash: Decimal
    cards: tuple[str, ...]
    wager: Decimal
    dealer_upcard: str | None
    legal_actions: tuple[str, ...]
    max_stake: Decimal
    split: bool
    running_count: int
    cards_remaining: int
    bet_limits: BetLimits = LEGACY_LIMITS


def observation(phase, player, shoe, *, hand=None, upcard=None,
                actions=(), max_stake=D(0), limits=LEGACY_LIMITS):
    return Observation(phase, player.cash, tuple(hand.cards) if hand else (),
                       hand.bet if hand else D(0), upcard, tuple(actions),
                       max_stake, hand.split if hand else False,
                       shoe.running_count, len(shoe.cards), limits)


def policy_stake(player, view):
    stake = player.policy(view)
    if (not isinstance(stake, Decimal) or not stake.is_finite()
            or stake < 0 or stake > view.max_stake
            or stake % D('.01')
            or (view.phase == 'bet' and not view.bet_limits.accepts(stake, view.cash))):
        raise ValueError('Policy must return a legal Decimal stake in cents.')
    return stake


@dataclass
class Hand:
    cards: list[str]
    bet: Decimal
    split: bool = False
    surrendered: bool = False


class Shoe:
    def __init__(self, rng=None):
        self.rng = rng if rng is not None else random
        self.cards = []
        self.shuffle()

    def shuffle(self):
        self.cards = list(RANKS) * 24  # Six decks, four suits per deck.
        self.rng.shuffle(self.cards)
        self.running_count = 0

    def draw(self):
        return self.cards.pop()

    def reveal(self, cards):
        """Update a Hi-Lo count only when cards become public."""
        for card in cards:
            if card in ('2', '3', '4', '5', '6'):
                self.running_count += 1
            elif card in ('10', 'J', 'Q', 'K', 'A'):
                self.running_count -= 1


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


def play_hand(player, hands, index, shoe, *, upcard=None, verbose=True,
              limits=LEGACY_LIMITS, on_update=None):
    emit = print if verbose else lambda *args: None
    def changed():
        if on_update is not None:
            on_update(index)

    hand = hands[index]
    if not hand.split and natural(hand.cards):
        return
    while total(hand.cards) < 21:
        changed()
        choices = ['hit', 'stand']
        if len(hand.cards) == 2 and player.cash >= hand.bet:
            choices.append('double')
            if hand.cards[0] == hand.cards[1] and len(hands) < 4:
                choices.append('split')
        if not hand.split and len(hand.cards) == 2:
            choices.append('surrender')
        emit(f'{player.name}, hand {index + 1}: {hand.cards} = {total(hand.cards)}; bet {money(hand.bet)}')
        if player.policy is None:
            action = input(f"Choose {' / '.join(choices)}: ").strip().lower()
        else:
            action = player.policy(observation('play', player, shoe, hand=hand,
                                               upcard=upcard, actions=choices, limits=limits))
            if action not in choices:
                raise ValueError(f'Policy returned an illegal action: {action!r}')
        if action not in choices:
            emit('Choose one of the displayed actions.')
            continue
        if action == 'stand':
            return
        if action == 'surrender':
            hand.surrendered = True
            changed()
            return
        if action == 'double':
            player.cash -= hand.bet
            hand.bet *= 2
            hand.cards.append(shoe.draw())
            shoe.reveal(hand.cards[-1:])
            changed()
            return
        if action == 'split':
            player.cash -= hand.bet
            rank = hand.cards[0]
            hand.cards = [rank, shoe.draw()]
            hand.split = True
            new_hand = Hand([rank, shoe.draw()], hand.bet, split=True)
            shoe.reveal([hand.cards[-1], new_hand.cards[-1]])
            hands.insert(index + 1, new_hand)
            changed()
            if rank == 'A':
                # Split aces receive exactly one card; no resplitting.
                return
            continue
        hand.cards.append(shoe.draw())
        shoe.reveal(hand.cards[-1:])
        changed()


def round_game(players, shoe, *, verbose=True, limits=LEGACY_LIMITS, observer=None):
    emit = print if verbose else lambda *args: None
    active = []
    dealer = []
    outcomes = {}

    def publish(phase, current=None, hand_index=None):
        """Give a UI public snapshots, never engine objects or the hidden card."""
        if observer is None:
            return
        hands_by_player = {id(player): hands for player, hands in active}
        revealed = phase == 'result'
        visible_dealer = list(dealer) if revealed else dealer[:1] + [None] * max(0, len(dealer) - 1)
        observer({
            'phase': phase,
            'players': [
                {'name': player.name, 'cash': str(player.cash), 'hands': [
                    {'cards': list(hand.cards), 'total': total(hand.cards),
                     'bet': str(hand.bet), 'split': hand.split,
                     'surrendered': hand.surrendered, 'outcome': outcomes.get(id(hand))}
                    for hand in hands_by_player.get(id(player), [])]}
                for player in players],
            'dealer': {'cards': visible_dealer,
                       'total': total(dealer) if revealed and dealer else None},
            'current_player': next((i for i, player in enumerate(players) if player is current), None),
            'current_hand': hand_index,
        })

    # A conservative cut card leaves enough for seven players with four hands.
    if len(shoe.cards) < 156:
        shoe.shuffle()
        emit('Shuffling the six-deck shoe.')
    for player in players:
        if player.cash < limits.minimum:
            emit(f'{player.name} has insufficient funds to bet.')
            continue
        emit(f'{player.name}: balance {money(player.cash)}')
        publish('bet', player)
        if player.policy is not None:
            bet = policy_stake(player, observation('bet', player, shoe,
                max_stake=limits.affordable_max(player.cash), limits=limits))
        else:
            cap = limits.affordable_max(player.cash)
            prompt = (f'Bet (0 to sit out; {money(limits.minimum)} minimum, '
                      f'{money(limits.increment)} increments, up to {money(cap)}): ')
            bet = amount(prompt, cap)
            while not limits.accepts(bet, player.cash):
                emit('Choose 0 or minimum + a whole number of increments within the limit.')
                bet = amount(prompt, cap)
        if bet:
            player.cash -= bet
            active.append((player, [Hand([], bet)]))
    if not active:
        emit('Everyone sat out. No cards dealt.')
        publish('result')
        return
    for _ in range(2):
        for _, hands in active:
            hands[0].cards.append(shoe.draw())
        dealer.append(shoe.draw())
    for _, hands in active:
        shoe.reveal(hands[0].cards)
    shoe.reveal(dealer[:1])
    publish('busy')
    emit(f'Dealer shows: {dealer[0]}')
    insurance = []
    if dealer[0] == 'A':
        for player, hands in active:
            emit(f'{player.name}: {hands[0].cards} = {total(hands[0].cards)}')
            cap = min(player.cash, hands[0].bet / 2)
            publish('insurance', player, 0)
            if player.policy is not None:
                stake = policy_stake(player, observation('insurance', player, shoe,
                    hand=hands[0], upcard=dealer[0], max_stake=cap, limits=limits))
            else:
                stake = amount(f'Insurance (0 to decline, up to {money(cap)}): ', cap)
            player.cash -= stake
            insurance.append((player, stake))
    dealer_blackjack = natural(dealer)
    for player, stake in insurance:
        if dealer_blackjack:
            player.cash += stake * 3
        if stake:
            emit(f'{player.name}: insurance {"wins 2:1" if dealer_blackjack else "loses"}.')
    if not dealer_blackjack:
        for player, hands in active:
            index = 0
            while index < len(hands):
                hand = hands[index]
                if not (hand.split and hand.cards[0] == 'A'):
                    play_hand(player, hands, index, shoe, upcard=dealer[0],
                              verbose=verbose, limits=limits,
                              on_update=lambda i: publish('play', player, i))
                index += 1
        needs_dealer = any(not h.surrendered and total(h.cards) <= 21
                           and not (natural(h.cards) and not h.split)
                           for _, hands in active for h in hands)
        if needs_dealer:
            while total(dealer) < 17:
                dealer.append(shoe.draw())
    shoe.reveal(dealer[1:])
    emit(f'Dealer: {dealer} = {total(dealer)}')
    for player, hands in active:
        for i, hand in enumerate(hands, 1):
            outcome = settle(player, hand, dealer)
            outcomes[id(hand)] = outcome
            emit(f'{player.name}, hand {i}: {hand.cards} ({total(hand.cards)}) — {outcome}')
        emit(f'{player.name}: balance {money(player.cash)}')
    publish('result')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_bet_arguments(parser)
    args = parser.parse_args()
    try:
        limits = BetLimits(args.min_bet, args.bet_increment, args.max_bet)
    except ValueError as error:
        parser.error(str(error))
    print('BLACKJACK — local multiplayer, 1–7 players vs dealer')
    print('Six decks | blackjack 3:2 | stand soft 17 | late surrender | insurance')
    print(f'Opening bets: minimum {money(limits.minimum)}, increments {money(limits.increment)}, '
          f'maximum {money(limits.maximum)} (0 to sit out).')
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
        round_game(players, shoe, limits=limits)
        if not any(p.cash >= limits.minimum for p in players):
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
