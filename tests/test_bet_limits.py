import json
from collections import deque
from decimal import Decimal as D
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blackjack import BetLimits, Hand, LEGACY_LIMITS, Observation, Player, Shoe, play_hand, round_game
from train_blackjack import (FlatPolicy, MonteCarloAgent, Settings, load_model,
                             save_model, session, stake_actions, summary, train)


class ScriptedPolicy:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.observations = []

    def __call__(self, view):
        self.observations.append(view)
        return self.responses.popleft()


def fixed_shoe(draws=()):
    shoe = Shoe(random.Random(0))
    shoe.cards = ['2'] * 160 + list(reversed(draws))
    return shoe


def bet_view(limits, funds, cap=None):
    funds = D(funds)
    return Observation('bet', funds, (), D(0), None, (),
                       limits.affordable_max(funds) if cap is None else D(cap),
                       False, 0, 300, limits)


class BetLimitTests(unittest.TestCase):
    def test_bets_start_at_the_minimum_then_follow_the_increment(self):
        limits = BetLimits(3, 2, 500)
        for stake in ('0', '3', '5', '499'):
            with self.subTest(legal=stake):
                self.assertTrue(limits.accepts(D(stake), D(1000)))
        for stake in ('1', '2', '4', '500', '501'):
            with self.subTest(illegal=stake):
                self.assertFalse(limits.accepts(D(stake), D(1000)))
        self.assertFalse(limits.accepts(D(5), D('4.99')))

    def test_caps_are_floored_to_an_affordable_legal_bet(self):
        limits = BetLimits(3, 2, '500.35')
        for funds, expected in [('1000', '499'), ('500', '499'),
                                ('6.99', '5'), ('3', '3'), ('2.99', '0')]:
            with self.subTest(funds=funds):
                self.assertEqual(limits.affordable_max(D(funds)), D(expected))
        self.assertEqual(BetLimits(5, '.50', 9999).affordable_max(D(20000)), D(9999))

    def test_invalid_configuration_rejects_inexact_half_bet_payouts(self):
        configurations = [
            ('0', '1', '500'), ('-3', '1', '500'), ('3', '0', '500'),
            ('3.01', '1', '500'), ('3', '.01', '500'),
            ('3', '.001', '500'), ('3', '1', '2.99'),
            ('NaN', '1', '500'), ('3', 'Infinity', '500'),
            ('3', '1', 'Infinity'), ('3', '1', '500.001'),
        ]
        for values in configurations:
            with self.subTest(values=values), self.assertRaises(ValueError):
                BetLimits(*values)

    def test_invalid_policy_bets_do_not_change_cash_or_deal_cards(self):
        limits = BetLimits(3, '.50', 500)
        for funds, stake in [('1000', '2.50'), ('1000', '3.25'),
                             ('1000', '500.50'), ('4', '4.50')]:
            with self.subTest(funds=funds, stake=stake):
                shoe = fixed_shoe()
                before = shoe.cards.copy()
                player = Player('P', D(funds), ScriptedPolicy(D(stake)))
                with self.assertRaises(ValueError):
                    round_game([player], shoe, verbose=False, limits=limits)
                self.assertEqual(player.cash, D(funds))
                self.assertEqual(shoe.cards, before)

    def test_minimum_increment_maximum_and_sit_out_are_accepted(self):
        limits = BetLimits(3, '.50', 500)
        for stake in (D(0), D(3), D('3.50'), D(500)):
            with self.subTest(stake=stake):
                # Player blackjack wins 3:2 without a play-action decision.
                policy = ScriptedPolicy(stake)
                player = Player('P', D(1000), policy)
                round_game([player], fixed_shoe(['A', '9', 'K', '8']),
                           verbose=False, limits=limits)
                self.assertEqual(player.cash, D(1000) + stake * D('1.5'))
                self.assertFalse(policy.responses)

    def test_observation_contains_rules_and_effective_cap(self):
        limits = BetLimits(3, 2, '500.35')
        for funds, cap in [('1000', '499'), ('126.50', '125')]:
            with self.subTest(funds=funds):
                policy = ScriptedPolicy(D(0))
                round_game([Player('P', D(funds), policy)], fixed_shoe(),
                           verbose=False, limits=limits)
                self.assertEqual(policy.observations[0].bet_limits, limits)
                self.assertEqual(policy.observations[0].max_stake, D(cap))

    def test_interactive_bet_reprompts_below_minimum_and_off_grid(self):
        player = Player('P', D(100))
        with patch('builtins.input', side_effect=['2', '4', '3', 'stand']) as prompt:
            round_game([player], fixed_shoe(['K', '10', 'Q', '7']),
                       verbose=False, limits=BetLimits(3, 2, 500))
        self.assertEqual(prompt.call_count, 4)
        self.assertEqual(player.cash, D(103))

    def test_players_below_minimum_are_skipped_without_a_decision(self):
        player = Player('P', D('2.99'), ScriptedPolicy())
        shoe = fixed_shoe()
        before = shoe.cards.copy()
        with patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            round_game([player], shoe, verbose=False, limits=BetLimits(3, '.50', 500))
        self.assertEqual(player.cash, D('2.99'))
        self.assertEqual(player.policy.observations, [])
        self.assertEqual(shoe.cards, before)

    def test_insurance_can_be_half_a_minimum_opening_bet(self):
        limits = BetLimits(5, 1, 500)
        policy = ScriptedPolicy(D(5), D('2.50'))
        player = Player('P', D(100), policy)
        round_game([player], fixed_shoe(['10', 'A', '9', 'K']),
                   verbose=False, limits=limits)
        self.assertEqual(player.cash, D(100))
        self.assertEqual(policy.observations[-1].phase, 'insurance')
        self.assertEqual(policy.observations[-1].max_stake, D('2.50'))

    def test_doubling_a_maximum_opening_bet_is_allowed(self):
        limits = BetLimits(5, 1, 500)
        policy = ScriptedPolicy(D(500), 'double')
        player = Player('P', D(2000), policy)
        round_game([player], fixed_shoe(['5', '10', '6', '7', 'K']),
                   verbose=False, limits=limits)
        self.assertEqual(player.cash, D(3000))
        self.assertEqual(policy.observations[-1].bet_limits, limits)

    def test_splitting_can_add_another_maximum_bet(self):
        player = Player('P', D(1500), ScriptedPolicy('split', 'stand'))
        hands = [Hand(['8', '8'], D(500))]
        play_hand(player, hands, 0, fixed_shoe(['K', '9']),
                  upcard='6', verbose=False, limits=BetLimits(5, 1, 500))
        self.assertEqual([hand.bet for hand in hands], [D(500), D(500)])
        self.assertEqual(player.cash, D(1000))

    def test_game_cli_accepts_custom_casino_limits(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(root / 'blackjack.py'), '--min-bet', '3',
             '--bet-increment', '.50', '--max-bet', '9999'],
            input='1\nTester\n10\n0\nn\n', capture_output=True,
            text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('minimum $3.00, increments $0.50, maximum $9,999.00', result.stdout)
        self.assertIn('Tester: $10.00', result.stdout)


class TrainingBetLimitTests(unittest.TestCase):
    def test_training_choices_obey_limits_and_still_offer_small_and_maximum_bets(self):
        limits = BetLimits(3, 2, '500.35')
        for funds in ('2.99', '3', '4.99', '7.25', '1000'):
            with self.subTest(funds=funds):
                view = bet_view(limits, funds)
                actions = [D(value) / 100 for value in stake_actions(view, D(10))]
                self.assertIn(D(0), actions)
                self.assertEqual(len(actions), len(set(actions)))
                self.assertTrue(all(limits.accepts(action, view.cash) for action in actions))
                self.assertIn(view.max_stake, actions)
                if D(funds) >= limits.minimum:
                    self.assertIn(limits.minimum, actions)
        self.assertEqual(stake_actions(bet_view(limits, '1000'), D(10)),
                         ['0', '300', '900', '1900', '4900', '49900'])

    def test_flat_policy_snaps_to_the_grid_and_respects_observed_cap(self):
        limits = BetLimits(3, 2, 500)
        policy = FlatPolicy(D(10))
        self.assertEqual(policy(bet_view(limits, '1000')), D(9))
        self.assertEqual(policy(bet_view(limits, '1000', cap='7')), D(7))
        self.assertEqual(policy(bet_view(limits, '2.99')), D(0))

    def test_insurance_actions_are_not_constrained_by_opening_minimum(self):
        view = Observation('insurance', D(95), ('10', '9'), D(5), 'A', (),
                           D('2.50'), False, 0, 300, BetLimits(5, 1, 500))
        self.assertEqual(stake_actions(view, D(10)), ['0', '250'])

    def test_session_ends_when_remaining_cash_cannot_meet_the_minimum(self):
        settings = Settings(bankroll='8', rounds=20, min_bet='5',
                            bet_increment='1', max_bet='500')
        policy = ScriptedPolicy(D(5), 'stand')
        with patch('train_blackjack.Shoe', return_value=fixed_shoe(['10', 'K', '6', 'Q'])), \
                patch('train_blackjack.round_game', wraps=round_game) as play_round:
            final, wagers = session(settings, policy, seed=0)
        self.assertEqual((final, wagers), (D(3), 1))
        self.assertEqual(play_round.call_count, 1)

    def test_ruin_threshold_is_the_configured_minimum(self):
        settings = Settings(bankroll='10', min_bet='5', bet_increment='1', max_bet='500')
        report = summary([D('4.99'), D(5), D(0)], [1, 1, 1], settings)
        self.assertEqual(report['ruin_rate'], 2 / 3)

    def test_custom_limit_models_train_and_roundtrip(self):
        settings = Settings(bankroll='25', unit_bet='4', rounds=4, players=3,
                            min_bet='3', bet_increment='.50', max_bet='10')
        agent = train(settings, episodes=12, seed=51)
        self.assertTrue(agent.table)
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / 'model.json'
            save_model(agent, model, episodes=12, seed=51)
            restored = load_model(model)
        self.assertEqual(restored.settings, settings)
        self.assertEqual(restored.settings.limits, BetLimits(3, '.50', 10))
        self.assertEqual(restored.table, agent.table)

    def test_version_one_models_without_limit_fields_keep_legacy_actions(self):
        agent = MonteCarloAgent(Settings())
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / 'old-model.json'
            save_model(agent, model, episodes=1, seed=1)
            data = json.loads(model.read_text())
            for field in ('min_bet', 'bet_increment', 'max_bet'):
                data['settings'].pop(field)
            model.write_text(json.dumps(data))
            restored = load_model(model)
        self.assertEqual(restored.settings.limits, LEGACY_LIMITS)
        self.assertEqual(stake_actions(bet_view(LEGACY_LIMITS, '100'), D(10)),
                         ['0', '1000', '2000', '5000'])

    def test_training_cli_saves_default_casino_limits(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / 'model.json'
            report = Path(folder) / 'report.json'
            result = subprocess.run(
                [sys.executable, str(root / 'train_blackjack.py'), 'train',
                 '--bankroll', '10', '--rounds', '2', '--episodes', '2',
                 '--eval-sessions', '2', '--model', str(model), '--report', str(report)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_model(model).settings.limits, BetLimits(5, 1, 500))


if __name__ == '__main__':
    unittest.main()
