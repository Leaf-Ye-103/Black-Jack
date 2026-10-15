import random
import unittest
from collections import deque
from dataclasses import FrozenInstanceError
from decimal import Decimal as D
from unittest.mock import patch

from blackjack import Hand, Observation, Player, Shoe, play_hand, round_game, total


class ScriptedPolicy:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.observations = []

    def __call__(self, observation):
        self.observations.append(observation)
        return self.responses.popleft()


def controlled_shoe(draws):
    shoe = Shoe(random.Random(0))
    # round_game leaves at least half a shoe before considering a shuffle.
    shoe.cards = ['2'] * 160 + list(reversed(draws))
    return shoe


class PolicyTests(unittest.TestCase):
    def test_policy_matches_interactive_rounds(self):
        cases = [
            ('hit', ['10', '6', '2', '10', '9', '2'],
             [D('10'), 'hit'], D('110')),
            ('double', ['5', '6', '6', '10', 'K', '2'],
             [D('10'), 'double'], D('120')),
            ('surrender', ['10', '9', '6', '8'],
             [D('10'), 'surrender'], D('95')),
            ('split and double', ['8', '6', '8', '10', '3', '2', '10', '2'],
             [D('10'), 'split', 'double', 'stand'], D('110')),
            ('split aces', ['A', '6', 'A', '10', 'K', '9', '2'],
             [D('10'), 'split'], D('120')),
            ('blackjack', ['A', '9', 'K', '8'],
             [D('10')], D('115')),
            ('winning insurance', ['10', 'A', '9', 'K'],
             [D('10'), D('5')], D('100')),
            ('losing insurance', ['10', 'A', '9', '7'],
             [D('10'), D('5'), 'stand'], D('105')),
        ]
        for name, draws, responses, expected_cash in cases:
            with self.subTest(name=name):
                interactive = Player('Interactive', D('100'))
                interactive_shoe = controlled_shoe(draws)
                with patch('builtins.input', side_effect=list(map(str, responses))) as prompt:
                    round_game([interactive], interactive_shoe, verbose=False)
                self.assertEqual(prompt.call_count, len(responses))

                policy = ScriptedPolicy(*responses)
                automated = Player('Automated', D('100'), policy=policy)
                automated_shoe = controlled_shoe(draws)
                with patch('builtins.input', side_effect=AssertionError('Unexpected input')):
                    round_game([automated], automated_shoe, verbose=False)

                self.assertFalse(policy.responses)
                self.assertEqual(automated.cash, expected_cash)
                self.assertEqual(automated.cash, interactive.cash)
                self.assertEqual(automated_shoe.cards, interactive_shoe.cards)
                self.assertEqual(automated_shoe.running_count,
                                 interactive_shoe.running_count)

    def test_multiple_automated_players_and_sitting_out(self):
        policies = [ScriptedPolicy(D('10'), 'stand'),
                    ScriptedPolicy(D('20'), 'stand'), ScriptedPolicy(D('0'))]
        players = [Player(str(i), D('100'), policy=policy)
                   for i, policy in enumerate(policies)]
        shoe = controlled_shoe(['K', '9', '10', 'Q', '10', '7'])
        round_game(players, shoe, verbose=False)
        self.assertEqual([player.cash for player in players],
                         [D('110'), D('120'), D('100')])
        self.assertEqual([view.phase for view in policies[2].observations], ['bet'])
        self.assertEqual(policies[0].observations[1].cards, ('K', 'Q'))
        self.assertEqual(policies[1].observations[1].cards, ('9', '10'))
        self.assertTrue(all(not policy.responses for policy in policies))

    def test_everyone_sitting_out_preserves_cards_and_count(self):
        shoe = controlled_shoe(['K', 'A'])
        shoe.reveal(['2', '3'])
        cards_before = shoe.cards.copy()
        policy = ScriptedPolicy(D('0'))
        player = Player('P', D('100'), policy=policy)
        round_game([player], shoe, verbose=False)
        self.assertEqual(player.cash, D('100'))
        self.assertEqual(shoe.cards, cards_before)
        self.assertEqual(shoe.running_count, 2)

    def test_hole_card_hidden_until_round_finishes(self):
        views = []
        counts = []
        for hole_card in ['10', '2']:
            policy = ScriptedPolicy(D('10'), 'surrender')
            shoe = controlled_shoe(['10', '6', '6', hole_card])
            round_game([Player('P', D('100'), policy=policy)], shoe, verbose=False)
            views.append(policy.observations[-1])
            counts.append(shoe.running_count)

        # Identical public observations despite different dealer hole cards.
        self.assertEqual(views[0], views[1])
        self.assertEqual(views[0].cards, ('10', '6'))
        self.assertEqual(views[0].dealer_upcard, '6')
        self.assertEqual(views[0].running_count, 1)
        self.assertEqual(views[0].cards_remaining, 160)
        self.assertEqual(counts, [0, 2])

    def test_insurance_observation_excludes_dealer_hole_card(self):
        policy = ScriptedPolicy(D('10'), D('0'))
        shoe = controlled_shoe(['5', 'A', '6', 'K'])
        round_game([Player('P', D('100'), policy=policy)], shoe, verbose=False)
        bet, insurance = policy.observations
        self.assertEqual(bet.phase, 'bet')
        self.assertEqual(bet.cash, D('100'))
        self.assertEqual(bet.max_stake, D('100'))
        self.assertIsNone(bet.dealer_upcard)
        self.assertEqual(bet.cards, ())
        self.assertEqual(insurance.phase, 'insurance')
        self.assertEqual(insurance.cash, D('90'))
        self.assertEqual(insurance.max_stake, D('5'))
        self.assertEqual(insurance.wager, D('10'))
        self.assertEqual(insurance.cards, ('5', '6'))
        self.assertEqual(insurance.dealer_upcard, 'A')
        self.assertEqual(insurance.running_count, 1)
        self.assertEqual(shoe.running_count, 0)

    def test_observations_are_immutable_public_snapshots(self):
        policy = ScriptedPolicy('hit', 'stand')
        player = Player('P', D('90'), policy=policy)
        hands = [Hand(['2', '3'], D('10'))]
        shoe = controlled_shoe(['4'])
        play_hand(player, hands, 0, shoe, upcard='6', verbose=False)
        first, second = policy.observations
        self.assertIsInstance(first, Observation)
        self.assertEqual(first.cards, ('2', '3'))
        self.assertEqual(second.cards, ('2', '3', '4'))
        self.assertEqual(first.running_count, 0)
        self.assertEqual(second.running_count, 1)
        self.assertEqual(first.dealer_upcard, '6')
        with self.assertRaises(FrozenInstanceError):
            first.cash = D('1000000')
        self.assertFalse(hasattr(first, 'shoe'))
        self.assertFalse(hasattr(first, 'dealer_hole_card'))

    def test_split_updates_public_state_and_available_actions(self):
        policy = ScriptedPolicy('split', 'double')
        player = Player('P', D('20'), policy=policy)
        hands = [Hand(['8', '8'], D('10'))]
        shoe = controlled_shoe(['3', '2', 'K'])
        play_hand(player, hands, 0, shoe, upcard='6', verbose=False)
        before, after = policy.observations
        self.assertIn('split', before.legal_actions)
        self.assertIn('surrender', before.legal_actions)
        self.assertTrue(after.split)
        self.assertEqual(after.cash, D('10'))
        self.assertEqual(after.cards, ('8', '3'))
        self.assertEqual(after.running_count, 2)
        self.assertIn('double', after.legal_actions)
        self.assertNotIn('surrender', after.legal_actions)
        self.assertEqual(player.cash, D('0'))
        self.assertEqual(hands[0].bet, D('20'))
        self.assertEqual(hands[0].cards, ['8', '3', 'K'])
        self.assertEqual(hands[1].cards, ['8', '2'])

    def test_play_actions_respect_available_funds(self):
        policy = ScriptedPolicy('stand')
        player = Player('P', D('9.98'), policy=policy)
        play_hand(player, [Hand(['8', '8'], D('10'))], 0,
                  controlled_shoe([]), upcard='6', verbose=False)
        self.assertEqual(policy.observations[0].legal_actions,
                         ('hit', 'stand', 'surrender'))

    def test_running_count_changes_only_on_reveal_and_resets_on_shuffle(self):
        shoe = controlled_shoe(['2', 'A', '7'])
        dealt = [shoe.draw(), shoe.draw(), shoe.draw()]
        self.assertEqual(shoe.running_count, 0)
        shoe.reveal(dealt[:1])
        self.assertEqual(shoe.running_count, 1)
        shoe.reveal(dealt[1:])
        self.assertEqual(shoe.running_count, 0)
        shoe.reveal(['2', '3', '4', '5', '6', '10', 'J', 'Q', 'K', 'A'])
        self.assertEqual(shoe.running_count, 0)
        shoe.reveal(['5'])
        shoe.shuffle()
        self.assertEqual(shoe.running_count, 0)
        self.assertEqual(len(shoe.cards), 312)

    def test_cut_card_resets_count_before_betting(self):
        shoe = controlled_shoe([])
        shoe.cards = ['2'] * 155
        shoe.reveal(['2', '3'])
        policy = ScriptedPolicy(D('0'))
        round_game([Player('P', D('100'), policy=policy)], shoe, verbose=False)
        view = policy.observations[0]
        self.assertEqual(view.phase, 'bet')
        self.assertEqual(view.running_count, 0)
        self.assertEqual(view.cards_remaining, 312)

    def test_seeded_automated_sessions_are_reproducible(self):
        def policy(view):
            if view.phase == 'bet':
                return min(D('2'), view.max_stake)
            if view.phase == 'insurance':
                return D('0')
            return 'hit' if total(view.cards) < 17 else 'stand'

        def run_session(seed):
            shoe = Shoe(random.Random(seed))
            players = [Player('One', D('100'), policy=policy),
                       Player('Two', D('100'), policy=policy)]
            history = []
            for _ in range(50):
                round_game(players, shoe, verbose=False)
                history.append((tuple(player.cash for player in players),
                                shoe.running_count, tuple(shoe.cards)))
            return history

        self.assertEqual(run_session(1234), run_session(1234))
        self.assertNotEqual(run_session(1234), run_session(4321))

    def test_invalid_bets_fail_before_cash_or_cards_change(self):
        stakes = [10, 10.0, '10', None, D('NaN'), D('Infinity'),
                  D('-0.02'), D('100.02'), D('0.01'), D('0.001')]
        for stake in stakes:
            with self.subTest(stake=repr(stake)):
                shoe = controlled_shoe([])
                before = shoe.cards.copy()
                player = Player('P', D('100'), policy=ScriptedPolicy(stake))
                with self.assertRaises(ValueError):
                    round_game([player], shoe, verbose=False)
                self.assertEqual(player.cash, D('100'))
                self.assertEqual(shoe.cards, before)

    def test_invalid_insurance_stakes_fail(self):
        for stake in [D('5.01'), D('-0.01'), D('0.001'), '5', D('NaN')]:
            with self.subTest(stake=repr(stake)):
                policy = ScriptedPolicy(D('10'), stake)
                player = Player('P', D('100'), policy=policy)
                shoe = controlled_shoe(['10', 'A', '9', 'K'])
                with self.assertRaises(ValueError):
                    round_game([player], shoe, verbose=False)
                self.assertEqual(player.cash, D('90'))

    def test_invalid_play_actions_fail_without_mutating_hand_or_cash(self):
        for action in ['split', 'dance', 'STAND', None, 1]:
            with self.subTest(action=action):
                player = Player('P', D('90'), policy=ScriptedPolicy(action))
                hand = Hand(['10', '6'], D('10'))
                shoe = controlled_shoe(['2'])
                cards_before = shoe.cards.copy()
                with self.assertRaises(ValueError):
                    play_hand(player, [hand], 0, shoe, upcard='6', verbose=False)
                self.assertEqual(player.cash, D('90'))
                self.assertEqual(hand.cards, ['10', '6'])
                self.assertEqual(shoe.cards, cards_before)


if __name__ == '__main__':
    unittest.main()
