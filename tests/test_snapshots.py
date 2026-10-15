from collections import deque
import copy
from decimal import Decimal as D
import json
import random
import unittest

from blackjack import Player, Shoe, round_game


def fixed_shoe(draws=()):
    shoe = Shoe(random.Random(0))
    shoe.cards = ['2'] * 160 + list(reversed(draws))
    return shoe


def scripted(*responses):
    choices = deque(responses)
    return lambda view: choices.popleft()


class SnapshotTests(unittest.TestCase):
    def test_hole_card_and_dealer_total_stay_hidden_until_result(self):
        snapshots = []
        player = Player('P', D(100), scripted(D(10), D(0), 'hit'))
        round_game([player], fixed_shoe(['10', 'A', '6', '7', '5']),
                   verbose=False, observer=snapshots.append)

        self.assertIn('insurance', [snapshot['phase'] for snapshot in snapshots])
        self.assertIn('play', [snapshot['phase'] for snapshot in snapshots])
        for snapshot in snapshots[:-1]:
            with self.subTest(phase=snapshot['phase']):
                self.assertIsNone(snapshot['dealer']['total'])
                self.assertEqual(snapshot['dealer']['cards'],
                                 [] if snapshot['phase'] == 'bet' else ['A', None])
        self.assertEqual(snapshots[-1]['phase'], 'result')
        self.assertEqual(snapshots[-1]['dealer'], {'cards': ['A', '7'], 'total': 18})
        # Every event can go directly to a browser without Decimal conversion.
        self.assertEqual(json.loads(json.dumps(snapshots)), snapshots)

    def test_different_hole_cards_produce_identical_public_events(self):
        histories = []
        for hole in ('10', '2'):
            snapshots = []
            round_game([Player('P', D(100), scripted(D(10), 'surrender'))],
                       fixed_shoe(['10', '6', '6', hole]), verbose=False,
                       observer=snapshots.append)
            histories.append(snapshots)
        self.assertEqual(histories[0][:-1], histories[1][:-1])
        self.assertNotEqual(histories[0][-1]['dealer'], histories[1][-1]['dealer'])

    def test_snapshots_are_detached_from_later_changes_and_each_other(self):
        snapshots, originals = [], []

        def observe(snapshot):
            snapshots.append(snapshot)
            originals.append(copy.deepcopy(snapshot))

        player = Player('P', D(100), scripted(D(10), 'hit', 'hit'))
        round_game([player], fixed_shoe(['2', '10', '3', '7', 'K', '6']),
                   verbose=False, observer=observe)
        self.assertEqual(snapshots, originals)
        initial_deal = next(s for s in snapshots if s['phase'] == 'busy')
        self.assertEqual(initial_deal['players'][0]['hands'][0]['cards'], ['2', '3'])
        self.assertEqual(snapshots[-1]['players'][0]['hands'][0]['cards'],
                         ['2', '3', 'K', '6'])

        initial_deal['players'][0]['hands'][0]['cards'].append('A')
        initial_deal['players'][0]['cash'] = '999999'
        initial_deal['dealer']['cards'][0] = 'A'
        self.assertEqual(snapshots[-1], originals[-1])
        self.assertEqual(player.cash, D(110))

    def test_active_indices_use_seats_and_track_both_split_hands(self):
        snapshots = []
        players = [Player('Sitting out', D(100), scripted(D(0))),
                   Player('Splitting', D(100), scripted(D(10), 'split', 'stand', 'stand')),
                   Player('Standing', D(100), scripted(D(20), 'stand'))]
        # Active players get 8/8 and K/Q against dealer 17; split to 18 and 17.
        round_game(players, fixed_shoe(['8', 'K', '10', '8', 'Q', '7', 'K', '9']),
                   verbose=False, observer=snapshots.append)

        bet_seats = [s['current_player'] for s in snapshots if s['phase'] == 'bet']
        self.assertEqual(bet_seats, [0, 1, 2])
        play_positions = {(s['current_player'], s['current_hand'])
                          for s in snapshots if s['phase'] == 'play'}
        self.assertEqual(play_positions, {(1, 0), (1, 1), (2, 0)})
        split_snapshot = next(s for s in snapshots if s['phase'] == 'play'
                              and len(s['players'][1]['hands']) == 2)
        self.assertEqual([h['cards'] for h in split_snapshot['players'][1]['hands']],
                         [['8', 'K'], ['8', '9']])
        final = snapshots[-1]
        self.assertEqual([D(p['cash']) for p in final['players']],
                         [player.cash for player in players])
        self.assertEqual([player.cash for player in players], [D(100), D(110), D(120)])
        self.assertEqual(final['players'][0]['hands'], [])
        self.assertEqual([h['outcome'] for h in final['players'][1]['hands']], ['win', 'push'])
        self.assertEqual(final['players'][2]['hands'][0]['outcome'], 'win')
        self.assertIsNone(final['current_player'])
        self.assertIsNone(final['current_hand'])

    def test_result_reports_surrender_and_final_cash(self):
        snapshots = []
        player = Player('P', D(100), scripted(D(10), 'surrender'))
        round_game([player], fixed_shoe(['10', '9', '6', '8']),
                   verbose=False, observer=snapshots.append)
        final_player = snapshots[-1]['players'][0]
        self.assertEqual(D(final_player['cash']), D(95))
        self.assertEqual(D(final_player['cash']), player.cash)
        self.assertTrue(final_player['hands'][0]['surrendered'])
        self.assertEqual(final_player['hands'][0]['outcome'], 'surrender (half bet returned)')

    def test_everyone_sitting_out_emits_empty_result_without_dealing(self):
        snapshots = []
        players = [Player('One', D(100), scripted(D(0))),
                   Player('Two', D(200), scripted(D(0)))]
        shoe = fixed_shoe(['A', 'K'])
        cards_before = shoe.cards.copy()
        round_game(players, shoe, verbose=False, observer=snapshots.append)
        self.assertEqual([s['phase'] for s in snapshots], ['bet', 'bet', 'result'])
        final = snapshots[-1]
        self.assertEqual(final['dealer'], {'cards': [], 'total': None})
        self.assertEqual([p['hands'] for p in final['players']], [[], []])
        self.assertEqual([D(p['cash']) for p in final['players']], [D(100), D(200)])
        self.assertIsNone(final['current_player'])
        self.assertIsNone(final['current_hand'])
        self.assertEqual(shoe.cards, cards_before)


if __name__ == '__main__':
    unittest.main()
