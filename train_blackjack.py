"""Learn betting and playing decisions by interacting with blackjack.py.

No labeled examples or third-party dependencies are needed. This is first-visit
Monte Carlo reinforcement learning, with an undiscounted final-bankroll reward.
"""
import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import random
import statistics
import sys

from blackjack import (BetLimits, LEGACY_LIMITS, Observation, Player, Shoe,
                       add_bet_arguments, money, round_game, total)

D = Decimal
MODEL_VERSION = 1
RULES = 'six-deck-S17-3:2-DAS-four-hands-late-surrender-v1'


def cents(value):
    return int(value * 100)


def cash(value):
    return D(value) / 100


@dataclass(frozen=True)
class Settings:
    bankroll: str = '1000.00'
    unit_bet: str = '10.00'
    rounds: int = 50
    players: int = 1
    # Missing fields in version-1 models retain their original betting rules.
    min_bet: str = '.02'
    bet_increment: str = '.02'
    max_bet: str | None = None

    def __post_init__(self):
        for name in ('bankroll', 'unit_bet'):
            try:
                value = D(getattr(self, name))
                if not value.is_finite() or value < D('.02') or value % D('.01'):
                    raise ValueError
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError(f'{name} must be a positive amount of at least $0.02 in cents.') from None
        if D(self.unit_bet) % D('.02'):
            raise ValueError('unit_bet must be a multiple of $0.02.')
        if not isinstance(self.rounds, int) or self.rounds < 1:
            raise ValueError('rounds must be a positive integer.')
        if not isinstance(self.players, int) or not 1 <= self.players <= 7:
            raise ValueError('players must be between 1 and 7.')
        self.limits  # Validate the complete opening-bet configuration.

    @property
    def limits(self):
        return BetLimits(self.min_bet, self.bet_increment, self.max_bet)


def stake_actions(view, unit):
    """Discrete action space; amounts are integer cents in the saved model."""
    cap = cents(view.max_stake)
    if view.phase == 'insurance':
        return ['0', str(cap)] if cap > 0 else ['0']
    if view.bet_limits == LEGACY_LIMITS:
        # Preserve the exact action/state keys used by existing version-1 models.
        cap -= cap % 2
        return [str(v) for v in sorted({0, *(min(cap, cents(unit) * k) for k in (1, 2, 5))})]
    funds = min(view.cash, view.max_stake)
    limits = view.bet_limits
    targets = [D(0), limits.minimum, limits.affordable_max(funds),
               *(unit * k for k in (1, 2, 5))]
    return [str(v) for v in sorted({cents(limits.snap(target, funds)) for target in targets})]


def fallback(view, actions):
    if view.phase != 'play':
        return '0'
    return 'stand' if total(view.cards) >= 17 else 'hit'


def bucket(value, boundaries):
    return sum(value >= b for b in boundaries)


class MonteCarloAgent:
    """Tabular control over compact, public-information state features.

    Each decision estimates (final cash - cash at that decision) / unit bet.
    Earlier money already spent is sunk and does not change action rankings.
    No reward clipping, win-rate proxy, or discounting changes the cash objective.
    """

    def __init__(self, settings, seed=0, table=None):
        self.settings = settings
        self.unit = D(settings.unit_bet)
        self.rng = random.Random(seed)
        self.table = table if table is not None else {}
        self.rounds_left = settings.rounds
        self.epsilon = 0.0
        self.learning = False
        self.trace = []
        self.decisions = 0
        self.unseen = 0
        self.verbose = False

    def state(self, view, actions):
        decks = max(1.0, view.cards_remaining / 52)
        count = max(-3, min(3, math.trunc(view.running_count / decks)))
        funds = bucket(float(view.cash / D(self.settings.bankroll)), (.25, .5, 1, 2, 4))
        horizon = bucket(self.rounds_left, (4, 11, 26))
        features = [view.phase, funds, horizon, count, ','.join(actions)]
        if view.phase != 'bet':
            # Tens and face cards have identical upcard values.
            upcard = '10' if view.dealer_upcard in ('J', 'Q', 'K') else view.dealer_upcard
            hard = sum(1 if c == 'A' else 10 if c in ('J', 'Q', 'K') else int(c)
                       for c in view.cards)
            pair = view.cards[0] if len(view.cards) == 2 and view.cards[0] == view.cards[1] else '-'
            features += [upcard, total(view.cards), int(total(view.cards) > hard),
                         pair, int(view.split), bucket(float(view.wager / self.unit), (1, 2, 5))]
        return json.dumps(features, separators=(',', ':'))

    def __call__(self, view):
        actions = list(view.legal_actions) if view.phase == 'play' else stake_actions(view, self.unit)
        key = self.state(view, actions)
        estimates = self.table.get(key, {})
        self.decisions += 1
        if not estimates:
            self.unseen += 1
        if self.learning and self.rng.random() < self.epsilon:
            action = self.rng.choice(actions)
        elif self.learning:
            # Optimistic zero for untried actions promotes exploration of losses.
            best = max(estimates.get(a, [0.0, 0])[0] for a in actions)
            action = self.rng.choice([a for a in actions if estimates.get(a, [0.0, 0])[0] == best])
        else:
            tried = [a for a in actions if a in estimates]
            action = max(tried, key=lambda a: estimates[a][0]) if tried else fallback(view, actions)
        if self.learning:
            self.trace.append((key, action, view.cash))
        if self.verbose:
            print(f'  Model chooses {view.phase}: {action if view.phase == "play" else money(cash(int(action)))}')
        return action if view.phase == 'play' else cash(int(action))

    def finish_session(self, final_cash):
        seen = set()
        for key, action, before in self.trace:
            if (key, action) in seen:
                continue
            seen.add((key, action))
            values = self.table.setdefault(key, {})
            estimate, visits = values.get(action, [0.0, 0])
            result = float((final_cash - before) / self.unit)
            visits += 1
            values[action] = [estimate + (result - estimate) / visits, visits]
        self.trace.clear()


class FlatPolicy:
    """Deliberately simple comparison player: flat bet and hit to 17."""
    def __init__(self, unit):
        self.unit = unit

    def __call__(self, view):
        if view.phase == 'bet':
            return view.bet_limits.snap(self.unit, min(view.cash, view.max_stake))
        if view.phase == 'insurance':
            return D(0)
        return 'stand' if total(view.cards) >= 17 else 'hit'


def sit_out(view):
    return D(0)


def session(settings, policy, seed, *, verbose=False):
    """One finite episode; opponents have their own non-replenished bankrolls."""
    shoe = Shoe(random.Random(seed))
    limits = settings.limits
    learner = Player('Model', D(settings.bankroll), policy)
    others = [Player(f'Player {i + 2}', D(settings.bankroll), FlatPolicy(D(settings.unit_bet)))
              for i in range(settings.players - 1)]
    bet_rounds = 0

    def track(view):
        nonlocal bet_rounds
        choice = policy(view)
        if view.phase == 'bet' and choice > 0:
            bet_rounds += 1
        return choice

    learner.policy = track
    for turn in range(settings.rounds):
        if learner.cash < limits.minimum:
            break
        if isinstance(policy, MonteCarloAgent):
            policy.rounds_left = settings.rounds - turn
        if verbose:
            print(f'\nRound {turn + 1}/{settings.rounds}')
        round_game([learner, *others], shoe, verbose=verbose, limits=limits)
    return learner.cash, bet_rounds


def train(settings, episodes, seed, *, progress=False):
    if episodes < 1:
        raise ValueError('episodes must be positive.')
    if D(settings.bankroll) < settings.limits.minimum:
        raise ValueError('Training bankroll must be at least the minimum opening bet.')
    agent = MonteCarloAgent(settings, seed=seed ^ 0xA11CE)
    agent.learning = True
    seeds = random.Random(seed)
    for episode in range(episodes):
        # Retain exploration throughout this finite training run.
        agent.epsilon = .05 + .95 * (1 - episode / max(1, episodes - 1)) ** 2
        final_cash, _ = session(settings, agent, seeds.getrandbits(64))
        agent.finish_session(final_cash)
        if progress and (episode + 1) % max(1, episodes // 10) == 0:
            print(f'Trained {episode + 1:,}/{episodes:,} sessions; {len(agent.table):,} states', file=sys.stderr)
    agent.learning = False
    agent.epsilon = 0.0
    return agent


def summary(finals, wagers, settings):
    base = D(settings.bankroll)
    profits = [float(f - base) for f in finals]
    mean = statistics.mean(profits)
    half = 1.96 * statistics.stdev(profits) / math.sqrt(len(profits)) if len(profits) > 1 else None
    return {
        'sessions': len(finals),
        'mean_final_bankroll': statistics.mean(float(f) for f in finals),
        'mean_profit': mean,
        'profit_95pct_normal_ci': [mean - half, mean + half] if half is not None else None,
        'median_final_bankroll': statistics.median(float(f) for f in finals),
        'profitable_session_rate': sum(p > 0 for p in profits) / len(finals),
        'losing_session_rate': sum(p < 0 for p in profits) / len(finals),
        'ruin_rate': sum(f < settings.limits.minimum for f in finals) / len(finals),
        'ruin_threshold': float(settings.limits.minimum),
        'mean_rounds_bet': statistics.mean(wagers),
    }


def evaluate(agent, sessions, seed):
    if sessions < 1:
        raise ValueError('evaluation sessions must be positive.')
    if agent.learning:
        raise ValueError('Freeze the agent before evaluation.')
    settings = agent.settings
    rng = random.Random(seed)
    seeds = [rng.getrandbits(64) for _ in range(sessions)]
    report = {'settings': asdict(settings), 'evaluation_seed': seed, 'policies': {}}
    initial_decisions, initial_unseen = agent.decisions, agent.unseen
    finals_by_policy = {}
    for name, policy in [('learned', agent), ('flat_hit_to_17', FlatPolicy(D(settings.unit_bet))),
                         ('sit_out', sit_out)]:
        outcomes = [session(settings, policy, s) for s in seeds]
        finals = [o[0] for o in outcomes]
        finals_by_policy[name] = finals
        report['policies'][name] = summary(finals, [o[1] for o in outcomes], settings)
    decisions = agent.decisions - initial_decisions
    report['unseen_state_decision_rate'] = (agent.unseen - initial_unseen) / decisions if decisions else 0.0
    report['learned_minus_baseline'] = {}
    for name in ('flat_hit_to_17', 'sit_out'):
        differences = [a - b for a, b in zip(finals_by_policy['learned'], finals_by_policy[name])]
        # Summarize paired differences directly, without treating them as bankrolls.
        values = [float(d) for d in differences]
        mean = statistics.mean(values)
        half = 1.96 * statistics.stdev(values) / math.sqrt(sessions) if sessions > 1 else None
        report['learned_minus_baseline'][name] = {
            'mean_final_bankroll_difference': mean,
            'difference_95pct_normal_ci': [mean - half, mean + half] if half is not None else None,
        }
    return report


def save_model(agent, path, *, episodes, seed):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'version': MODEL_VERSION, 'rules': RULES, 'algorithm': 'first-visit Monte Carlo control',
            'objective': 'maximize expected final bankroll over a fixed session',
            'settings': asdict(agent.settings), 'training_episodes': episodes,
            'training_seed': seed, 'table': agent.table}
    path.write_text(json.dumps(data, separators=(',', ':'), allow_nan=False) + '\n')


def load_model(path):
    data = json.loads(Path(path).read_text())
    if data.get('version') != MODEL_VERSION or data.get('rules') != RULES:
        raise ValueError('Unsupported model version or blackjack rules.')
    settings = Settings(**data['settings'])
    table = data['table']
    if not isinstance(table, dict):
        raise ValueError('Invalid model table.')
    for state, entries in table.items():
        if not isinstance(state, str) or not isinstance(entries, dict):
            raise ValueError('Invalid model state.')
        for action, value in entries.items():
            if (not isinstance(action, str) or not isinstance(value, list) or len(value) != 2
                    or not isinstance(value[0], (int, float)) or not math.isfinite(value[0])
                    or not isinstance(value[1], int) or value[1] < 1):
                raise ValueError('Invalid model estimate.')
    return MonteCarloAgent(settings, table=table)


def write_report(report, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


def display_report(report):
    print('\nHeld-out evaluation (no learning during evaluation):')
    print(f"Ruin means a bankroll below the minimum opening bet of "
          f"{money(Settings(**report['settings']).limits.minimum)}.")
    for name, result in report['policies'].items():
        interval = result['profit_95pct_normal_ci']
        ci = f'[{interval[0]:.2f}, {interval[1]:.2f}]' if interval else 'unavailable (one session)'
        print(f"{name}: mean final ${result['mean_final_bankroll']:.2f}; "
              f"mean profit ${result['mean_profit']:.2f}, approximate 95% CI {ci}; "
              f"ruin {result['ruin_rate']:.1%}; rounds bet {result['mean_rounds_bet']:.1f}")
    print(f"Unseen model states: {report['unseen_state_decision_rate']:.1%} of decisions.")
    print('An improvement or profit is not guaranteed. Sitting out preserves the initial bankroll.')


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Must be a positive integer.')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    trainer = commands.add_parser('train', help='Train from simulated experience and evaluate.')
    trainer.add_argument('--bankroll', default='1000.00', help='Starting cash for each session.')
    trainer.add_argument('--unit-bet', default='10.00',
                         help='Bet target and reward unit, in multiples of $0.02; targets are snapped to legal bets.')
    add_bet_arguments(trainer)
    trainer.add_argument('--rounds', type=positive_int, default=50)
    trainer.add_argument('--players', type=int, choices=range(1, 8), default=1)
    trainer.add_argument('--episodes', type=positive_int, default=10000)
    trainer.add_argument('--eval-sessions', type=positive_int, default=1000)
    trainer.add_argument('--seed', type=int, default=42)
    trainer.add_argument('--model', default='models/blackjack_model.json')
    trainer.add_argument('--report', default='reports/blackjack_evaluation.json')
    evaluator = commands.add_parser('evaluate', help='Evaluate using the saved model\'s table limits.')
    evaluator.add_argument('--model', default='models/blackjack_model.json')
    evaluator.add_argument('--sessions', type=positive_int, default=1000)
    evaluator.add_argument('--seed', type=int, default=1000042)
    evaluator.add_argument('--report', default='reports/blackjack_evaluation.json')
    player = commands.add_parser('play', help='Watch a session using the saved model\'s table limits.')
    player.add_argument('--model', default='models/blackjack_model.json')
    player.add_argument('--seed', type=int, default=2000042)
    args = parser.parse_args()
    try:
        if args.command == 'train':
            settings = Settings(args.bankroll, args.unit_bet, args.rounds, args.players,
                                args.min_bet, args.bet_increment, args.max_bet)
            agent = train(settings, args.episodes, args.seed, progress=True)
            save_model(agent, args.model, episodes=args.episodes, seed=args.seed)
            print(f'Saved model to {args.model}')
            report = evaluate(agent, args.eval_sessions, args.seed + 1000000)
        else:
            agent = load_model(args.model)
            if args.command == 'play':
                agent.verbose = True
                final, _ = session(agent.settings, agent, args.seed, verbose=True)
                print(f'Final model bankroll: {money(final)}; '
                      f'profit: {money(final - D(agent.settings.bankroll))}')
                return
            report = evaluate(agent, args.sessions, args.seed)
        write_report(report, args.report)
        display_report(report)
        print(f'Saved report to {args.report}')
    except (ValueError, TypeError, KeyError, OSError, InvalidOperation) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
