import copy
from decimal import Decimal as D
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blackjack import Observation, Shoe, round_game
from train_blackjack import (
    MonteCarloAgent,
    Settings,
    evaluate,
    load_model,
    save_model,
    session,
    sit_out,
    stake_actions,
    summary,
    train,
)


def view(*, phase='bet', cash='100.00', max_stake='100.00', cards=(),
         wager='0.00', upcard=None, actions=()):
    return Observation(phase, D(cash), tuple(cards), D(wager), upcard,
                       tuple(actions), D(max_stake), False, 0, 300)


def fixed_shoe(draws):
    shoe = Shoe(random.Random(0))
    # The padding keeps the engine's cut-card rule from replacing the test deal.
    shoe.cards = ['2'] * 160 + list(reversed(draws))
    return shoe


class TrainingTests(unittest.TestCase):
    def test_profit_reporting_subtracts_money_before_float_conversion(self):
        settings = Settings(bankroll='10000000000000000.00')
        report = summary([D('10000000000000000.02')], [1], settings)
        self.assertEqual(report['mean_profit'], .02)

    def test_sitting_out_still_finishes_a_finite_session(self):
        settings = Settings(bankroll='100.00', rounds=7, players=3)
        with patch('train_blackjack.round_game', wraps=round_game) as play_round:
            final_cash, wagers = session(settings, sit_out, seed=91)
        self.assertEqual(play_round.call_count, settings.rounds)
        self.assertEqual((final_cash, wagers), (D('100.00'), 0))

    def test_bankruptcy_ends_session_without_replenishing_cash(self):
        settings = Settings(bankroll='.02', unit_bet='.02', rounds=100)

        def all_in_then_stand(observation):
            return observation.max_stake if observation.phase == 'bet' else 'stand'

        # Player 16 loses to dealer 20 on the first round.
        shoe = fixed_shoe(['10', 'K', '6', 'Q'])
        with patch('train_blackjack.Shoe', return_value=shoe), \
                patch('train_blackjack.round_game', wraps=round_game) as play_round:
            final_cash, wagers = session(settings, all_in_then_stand, seed=0)
        self.assertEqual(play_round.call_count, 1)
        self.assertEqual((final_cash, wagers), (D('0.00'), 1))

    def test_session_uses_engine_to_credit_winning_bet(self):
        settings = Settings(bankroll='100.00', unit_bet='10.00', rounds=1)
        observations = []

        def flat_then_stand(observation):
            observations.append(observation)
            return D('10.00') if observation.phase == 'bet' else 'stand'

        # Player 20 beats dealer 17, with the actual engine deducting the stake.
        shoe = fixed_shoe(['K', '10', 'Q', '7'])
        with patch('train_blackjack.Shoe', return_value=shoe):
            final_cash, wagers = session(settings, flat_then_stand, seed=0)
        self.assertEqual((final_cash, wagers), (D('110.00'), 1))
        self.assertEqual([o.cash for o in observations], [D('100.00'), D('90.00')])
        self.assertEqual([o.phase for o in observations], ['bet', 'play'])

    def test_final_cash_return_is_exact_and_first_visit_only(self):
        agent = MonteCarloAgent(Settings(bankroll='100.00', unit_bet='10.00'))
        agent.learning = True
        decision = view(phase='play', cash='100.00', cards=('10', '7'),
                        wager='10.00', upcard='6', actions=('stand',))
        agent(decision)
        agent(decision)
        key = agent.state(decision, ['stand'])
        agent.finish_session(D('110.00'))
        # Final profit is $10 / $10, regardless of the number of decisions.
        self.assertEqual(agent.table[key]['stand'], [1.0, 1])
        self.assertEqual(agent.trace, [])

        agent(decision)
        agent.finish_session(D('80.00'))
        self.assertEqual(agent.table[key]['stand'], [-0.5, 2])

    def test_money_actions_stay_legal_with_low_or_odd_cent_bankroll(self):
        for bankroll in ('.02', '.03', '.19', '10.01', '999.99'):
            with self.subTest(bankroll=bankroll):
                choices = [D(a) / 100 for a in stake_actions(
                    view(cash=bankroll, max_stake=bankroll), D('10.00'))]
                self.assertIn(D(0), choices)
                self.assertTrue(all(D(0) <= a <= D(bankroll) for a in choices))
                self.assertTrue(all(a % D('.02') == 0 for a in choices))
                self.assertEqual(len(choices), len(set(choices)))

        insurance = view(phase='insurance', cash='.01', max_stake='.01',
                         cards=('10', '7'), wager='.02', upcard='A')
        self.assertEqual(stake_actions(insurance, D('10.00')), ['0', '1'])
        no_cash = view(phase='insurance', cash='0', max_stake='0',
                       cards=('10', '7'), wager='.02', upcard='A')
        self.assertEqual(stake_actions(no_cash, D('10.00')), ['0'])

    def test_training_is_reproducible_and_frozen_afterwards(self):
        settings = Settings(bankroll='25.00', unit_bet='2.00', rounds=6, players=3)
        first = train(settings, episodes=30, seed=821)
        second = train(settings, episodes=30, seed=821)
        self.assertTrue(first.table)
        self.assertEqual(first.table, second.table)
        self.assertFalse(first.learning)
        self.assertEqual(first.epsilon, 0.0)
        self.assertEqual(first.trace, [])

    def test_saved_model_roundtrip_and_evaluation_leave_table_frozen(self):
        settings = Settings(bankroll='25.00', unit_bet='2.00', rounds=5, players=2)
        agent = train(settings, episodes=20, seed=13)
        original = copy.deepcopy(agent.table)
        with tempfile.TemporaryDirectory() as folder:
            model_path = Path(folder) / 'models' / 'learned.json'
            save_model(agent, model_path, episodes=20, seed=13)
            restored = load_model(model_path)
            self.assertEqual(restored.settings, settings)
            self.assertEqual(restored.table, original)
            self.assertFalse(restored.learning)
            first_report = evaluate(restored, sessions=8, seed=1_000_013)
            second_report = evaluate(restored, sessions=8, seed=1_000_013)
            self.assertEqual(first_report, second_report)
            self.assertEqual(restored.table, original)
            self.assertEqual(agent.table, original)
            self.assertEqual(restored.trace, [])

    def test_evaluation_uses_held_out_shared_seeds_and_sit_out_baseline(self):
        settings = Settings(bankroll='37.50', unit_bet='2.00', rounds=5, players=2)
        with patch('train_blackjack.session', wraps=session) as simulated_session:
            agent = train(settings, episodes=7, seed=17)
            training_seeds = [call.args[2] for call in simulated_session.call_args_list]
        with patch('train_blackjack.session', wraps=session) as simulated_session:
            report = evaluate(agent, sessions=4, seed=1_000_017)
            evaluation_seeds = [call.args[2] for call in simulated_session.call_args_list]
        self.assertTrue(set(training_seeds).isdisjoint(evaluation_seeds))
        self.assertEqual(len(set(evaluation_seeds[:4])), 4)
        self.assertEqual(evaluation_seeds[:4], evaluation_seeds[4:8])
        self.assertEqual(evaluation_seeds[:4], evaluation_seeds[8:])
        self.assertEqual(report['evaluation_seed'], 1_000_017)
        baseline = report['policies']['sit_out']
        self.assertEqual(baseline['sessions'], 4)
        self.assertEqual(baseline['mean_final_bankroll'], 37.5)
        self.assertEqual(baseline['mean_profit'], 0)
        self.assertEqual(baseline['profit_95pct_normal_ci'], [0, 0])
        self.assertEqual(baseline['ruin_rate'], 0)
        self.assertEqual(baseline['mean_rounds_bet'], 0)
        self.assertEqual(baseline['profitable_session_rate'], 0)
        self.assertEqual(baseline['losing_session_rate'], 0)
        self.assertEqual(set(report['learned_minus_baseline']), {'flat_hit_to_17', 'sit_out'})
        self.assertGreaterEqual(report['unseen_state_decision_rate'], 0)
        self.assertLessEqual(report['unseen_state_decision_rate'], 1)

    def test_cli_trains_then_evaluates_saved_model_without_mutation(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            model_path = Path(folder) / 'model.json'
            report_path = Path(folder) / 'training-report.json'
            training = subprocess.run(
                [sys.executable, str(root / 'train_blackjack.py'), 'train',
                 '--bankroll', '20.00', '--unit-bet', '2.00', '--rounds', '3',
                 '--players', '2', '--episodes', '12', '--eval-sessions', '4',
                 '--seed', '22', '--model', str(model_path), '--report', str(report_path)],
                cwd=folder, text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(training.returncode, 0, training.stderr)
            saved = model_path.read_bytes()
            report = json.loads(report_path.read_text())
            self.assertEqual(report['evaluation_seed'], 1_000_022)
            self.assertEqual(report['policies']['learned']['sessions'], 4)
            evaluation_report = Path(folder) / 'evaluation-report.json'
            evaluation = subprocess.run(
                [sys.executable, str(root / 'train_blackjack.py'), 'evaluate',
                 '--model', str(model_path), '--sessions', '4', '--seed', '1000022',
                 '--report', str(evaluation_report)],
                cwd=folder, text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(evaluation.returncode, 0, evaluation.stderr)
            self.assertEqual(model_path.read_bytes(), saved)
            self.assertEqual(json.loads(evaluation_report.read_text()), report)


if __name__ == '__main__':
    unittest.main()
