"""Blackjack rules, terminal play, and browser table. Requires Python 3.10+.

The trainer imports this module to use the same rules and cash accounting.
Run ``python3 blackjack.py web`` for the browser or ``python3 blackjack.py play``
for the terminal. Importing this module never starts a game or a server.
"""
import argparse
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import random
import re
import sys
import threading
from typing import Callable
from urllib.parse import urlsplit

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


def play_terminal(limits=CASINO_LIMITS):
    """Run interactive play using the shared game rules."""
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


class APIError(ValueError):
    """An invalid browser request, without changing the running round."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Stopped(Exception):
    pass


_NO_ANSWER = object()
_CENT = Decimal('.01')
_MONEY = re.compile(r'(?:\d{1,18}(?:\.\d{1,2})?|\.\d{1,2})\Z', re.ASCII)


def _amount(value, label):
    """Only accept bounded decimal strings, never JSON floats or exponents."""
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise APIError(f'{label} must be a nonnegative amount with at most two decimal places.')
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number % _CENT:
            raise InvalidOperation
    except InvalidOperation:
        raise APIError(f'{label} must be a valid amount in cents.') from None
    return number


def _limits_json(limits):
    return {name: str(getattr(limits, name))
            for name in ('minimum', 'increment', 'maximum')}


class Table:
    """A single shared local table and one paused engine worker per round.

    A condition variable pauses the synchronous engine at policy decisions. A
    one-use prompt ID makes double-clicks and requests from stale tabs harmless.
    Only public observer snapshots are sent back to browsers.
    """

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._revision = 0
        self._prompt_id = 0
        self._round = 0
        self._players = []
        self._shoe = Shoe()
        self._limits = CASINO_LIMITS
        self._snapshot = {'players': [], 'dealer': {'cards': [], 'total': None},
                          'current_player': None, 'current_hand': None}
        self._phase = 'setup'
        self._message = 'Create a table with 1–7 players to begin.'
        self._prompt = None
        self._pending = None
        self._answer = _NO_ANSWER
        self._active = False
        self._closed = False
        self._thread = None

    def _changed(self):
        self._revision += 1
        self._condition.notify_all()

    def state(self):
        with self._condition:
            state = deepcopy(self._snapshot)
            state.update(revision=self._revision, phase=self._phase,
                         round=self._round, prompt=deepcopy(self._prompt),
                         limits=_limits_json(self._limits), message=self._message)
            return state

    def new(self, config):
        if not isinstance(config, dict):
            raise APIError('Table configuration must be a JSON object.')
        players = config.get('players')
        if not isinstance(players, list) or not 1 <= len(players) <= 7:
            raise APIError('Choose between 1 and 7 players.')
        specifications = []
        for index, player in enumerate(players):
            if not isinstance(player, dict):
                raise APIError('Each player needs a name and starting cash.')
            name = player.get('name', f'Player {index + 1}')
            if not isinstance(name, str) or len(name) > 40:
                raise APIError('Player names must be text of at most 40 characters.')
            name = name.strip() or f'Player {index + 1}'
            if any(ord(character) < 32 for character in name):
                raise APIError('Player names cannot contain control characters.')
            specifications.append((name, _amount(player.get('cash'), 'Starting cash')))
        settings = config.get('limits', {})
        if not isinstance(settings, dict):
            raise APIError('Bet limits must be a JSON object.')
        values = {}
        for key in ('minimum', 'increment', 'maximum'):
            value = settings.get(key, str(getattr(CASINO_LIMITS, key)))
            values[key] = _amount(value, f'Bet {key}')
        try:
            limits = BetLimits(**values)
        except ValueError as error:
            raise APIError(str(error)) from None
        with self._condition:
            self._require_open()
            if self._active:
                raise APIError('Finish the current round before creating a new table.', 409)
            self._players = [Player(name, cash,
                                    policy=lambda view, i=i: self._choose(i, view))
                             for i, (name, cash) in enumerate(specifications)]
            self._limits = limits
            self._shoe = Shoe()
            self._round = 0
            self._pending = None
            self._prompt = None
            self._answer = _NO_ANSWER
            self._snapshot = {
                'players': [{'name': p.name, 'cash': str(p.cash), 'hands': []}
                            for p in self._players],
                'dealer': {'cards': [], 'total': None},
                'current_player': None, 'current_hand': None,
            }
            self._phase = 'result'
            self._message = 'Table ready. Start the first round.'
            self._changed()
            return self.state()

    def _require_open(self):
        if self._closed:
            raise APIError('This table has closed.', 409)

    def start_round(self):
        with self._condition:
            self._require_open()
            if self._active:
                raise APIError('A round is already in progress.', 409)
            if not self._players:
                raise APIError('Create a table before starting a round.', 409)
            self._active = True
            self._round += 1
            self._phase = 'busy'
            self._message = 'Starting the round…'
            self._pending = None
            self._prompt = None
            self._snapshot = {
                'players': [{'name': p.name, 'cash': str(p.cash), 'hands': []}
                            for p in self._players],
                'dealer': {'cards': [], 'total': None},
                'current_player': None, 'current_hand': None,
            }
            self._changed()
            self._thread = threading.Thread(target=self._run_round,
                                            name='blackjack-round', daemon=True)
            self._thread.start()
            return self.state()

    def _observe(self, snapshot):
        with self._condition:
            if self._closed:
                raise _Stopped
            self._snapshot = deepcopy(snapshot)
            # Observer callbacks precede policy prompts and also follow draws.
            # Only _choose may publish an interactive phase with its prompt;
            # settlement becomes actionable after the worker releases the round.
            self._phase = 'busy'
            self._changed()

    def _choose(self, player_index, view):
        with self._condition:
            if self._closed:
                raise _Stopped
            self._prompt_id += 1
            self._pending = view
            self._answer = _NO_ANSWER
            self._phase = view.phase
            self._snapshot['current_player'] = player_index
            self._prompt = {
                'id': self._prompt_id, 'phase': view.phase,
                'max_stake': str(view.max_stake), 'cash': str(view.cash),
                'actions': list(view.legal_actions),
                'minimum': str(view.bet_limits.minimum if view.phase == 'bet' else Decimal(0)),
                'increment': str(view.bet_limits.increment if view.phase == 'bet' else _CENT),
            }
            name = self._players[player_index].name
            self._message = {
                'bet': f'{name}: place a bet, or choose 0 to sit out.',
                'insurance': f'{name}: choose insurance, or 0 to decline.',
                'play': f'{name}: choose an action.',
            }[view.phase]
            self._changed()
            self._condition.wait_for(lambda: self._answer is not _NO_ANSWER or self._closed)
            if self._closed:
                raise _Stopped
            answer = self._answer
            self._answer = _NO_ANSWER
            return answer

    def act(self, prompt_id, value):
        with self._condition:
            self._require_open()
            if (type(prompt_id) is not int or self._pending is None
                    or self._prompt is None or prompt_id != self._prompt['id']):
                raise APIError('That decision has expired. Refresh the current table.', 409)
            view = self._pending
            if view.phase == 'play':
                if not isinstance(value, str) or value not in view.legal_actions:
                    raise APIError('Choose one of the available actions.')
                answer = value
            else:
                answer = _amount(value, 'Wager')
                if answer > view.max_stake:
                    raise APIError('That wager exceeds the available cash or table limit.')
                if view.phase == 'bet' and not view.bet_limits.accepts(answer, view.cash):
                    raise APIError('Choose 0, or the minimum plus a whole number of bet increments.')
            self._answer = answer
            self._pending = None
            self._prompt = None
            self._phase = 'busy'
            self._message = 'Playing the round…'
            self._changed()
            return self.state()

    def _run_round(self):
        failure = None
        try:
            round_game(self._players, self._shoe, verbose=False,
                       limits=self._limits, observer=self._observe)
        except _Stopped:
            return
        except Exception as error:
            failure = str(error)
        finally:
            with self._condition:
                if not self._closed:
                    self._phase = 'error' if failure is not None else 'result'
                    any_hands = any(p['hands'] for p in self._snapshot['players'])
                    no_funds = all(p.cash < self._limits.minimum for p in self._players)
                    self._message = (
                        f'The round stopped: {failure}' if failure is not None else
                        'Round complete. Start another round when ready.' if any_hands else
                        'No players have enough funds for the minimum bet.' if no_funds else
                        'No bets were placed. No cards were dealt.')
                self._active = False
                self._pending = None
                self._prompt = None
                self._changed()

    def close(self):
        """Release a worker waiting on a human decision during server shutdown."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)


_WEB_ROOT = Path(__file__).resolve().parent / 'web'
_STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
           '/index.html': ('index.html', 'text/html; charset=utf-8'),
           '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
           '/style.css': ('style.css', 'text/css; charset=utf-8'),
           '/app.js': ('app.js', 'text/javascript; charset=utf-8')}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # State polling is frequent; startup and fatal errors use the CLI output.
        pass

    def _send(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode('utf-8'))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/state':
            self._json(200, self.server.table.state())
            return
        entry = _STATIC.get(path)
        if entry is None:
            self._json(404, {'error': 'Page not found.'})
            return
        filename, content_type = entry
        try:
            body = (_WEB_ROOT / filename).read_bytes()
        except OSError:
            self._json(404, {'error': 'Web asset not found.'})
            return
        self._send(200, body, content_type)

    def _read_json(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        if origin is not None and origin.lower() != f'http://{host}'.lower():
            raise APIError('Requests from another website are not allowed.', 403)
        if self.headers.get_content_type() != 'application/json':
            raise APIError('Send an application/json request.', 415)
        if self.headers.get('Transfer-Encoding'):
            raise APIError('Chunked requests are not supported.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise APIError('Invalid request length.') from None
        if length <= 0:
            raise APIError('A JSON request body is required.')
        if length > 16_384:
            raise APIError('Request body is too large.', 413)
        try:
            data = json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise APIError('Invalid JSON request body.') from None
        if not isinstance(data, dict):
            raise APIError('The request body must be a JSON object.')
        return data

    def do_POST(self):
        try:
            data = self._read_json()
            path = urlsplit(self.path).path
            table = self.server.table
            if path == '/api/new':
                result = table.new(data)
            elif path == '/api/round':
                result = table.start_round()
            elif path == '/api/action':
                result = table.act(data.get('prompt_id'), data.get('value'))
            else:
                raise APIError('API route not found.', 404)
            self._json(200, result)
        except APIError as error:
            self._json(error.status, {'error': str(error)})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        self.table.close()
        super().server_close()


def make_server(host='127.0.0.1', port=0, table=None):
    """Construct an unstarted server; port 0 selects an available test port."""
    server = _Server((host, port), _Handler)
    server.table = table if table is not None else Table()
    return server


def serve_web(host='127.0.0.1', port=8000):
    """Run the browser interface; training can import the engine without this."""
    server = make_server(host, port)
    print(f'Blackjack table: http://{host}:{server.server_address[1]}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nClosing the table.')
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    terminal = commands.add_parser('play', help='Play in the terminal.')
    add_bet_arguments(terminal)
    web = commands.add_parser('web', help='Start the browser table.')
    web.add_argument('--host', default='127.0.0.1', help='Bind address (default: 127.0.0.1).')
    web.add_argument('--port', default=8000, type=int, help='Port (default: 8000).')
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Preserve the existing terminal command, including its bet-limit flags.
    if not arguments or (arguments[0].startswith('-') and arguments[0] not in ('-h', '--help')):
        arguments.insert(0, 'play')
    args = parser.parse_args(arguments)
    if args.command == 'web':
        if not 0 <= args.port <= 65535:
            parser.error('port must be between 0 and 65535')
        serve_web(args.host, args.port)
    else:
        try:
            limits = BetLimits(args.min_bet, args.bet_increment, args.max_bet)
        except ValueError as error:
            parser.error(str(error))
        play_terminal(limits)


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\nGame closed.')
