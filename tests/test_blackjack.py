import unittest
from decimal import Decimal as D
from unittest.mock import patch
from blackjack import Player, Hand, Shoe, total, natural, settle, round_game, play_hand


class BlackjackTests(unittest.TestCase):
    def test_aces(self):
        self.assertEqual(total(['A', 'A', '9']), 21)
        self.assertEqual(total(['A', '6']), 17)
        self.assertEqual(total(['A', 'K', 'K']), 21)

    def test_payouts(self):
        cases = [(['A', 'K'], ['10', '9'], False, '125'),
                 (['A', 'K'], ['A', 'Q'], False, '110'),
                 (['A', 'K'], ['10', '9'], True, '120'),
                 (['10', '8'], ['10', '8'], False, '110'),
                 (['10', '8'], ['10', '7', '9'], False, '120'),
                 (['10', '8', '5'], ['10', '7', '9'], False, '100'),
                 (['10', '8'], ['A', 'K'], False, '100')]
        for cards, dealer, split, expected in cases:
            with self.subTest(cards=cards, dealer=dealer, split=split):
                p = Player('P', D(100))
                settle(p, Hand(cards, D(10), split), dealer)
                self.assertEqual(p.cash, D(expected))

    def test_surrender(self):
        p = Player('P', D(90))
        settle(p, Hand(['10', '6'], D(10), surrendered=True), ['10', '9'])
        self.assertEqual(p.cash, D(95))

    def test_multiplayer_round(self):
        shoe = Shoe()
        # Deal P1=20, P2=19, dealer=17.
        shoe.cards = ['2'] * 160 + list(reversed(['K', '9', '10', 'Q', '10', '7']))
        players = [Player('One', D(100)), Player('Two', D(100))]
        with patch('builtins.input', side_effect=['10', '20', 'stand', 'stand']):
            round_game(players, shoe)
        self.assertEqual([p.cash for p in players], [D(110), D(120)])

    def test_insurance_and_dealer_blackjack(self):
        shoe = Shoe()
        shoe.cards = ['2'] * 160 + list(reversed(['10', 'A', '9', 'K']))
        p = Player('P', D(100))
        with patch('builtins.input', side_effect=['10', '5']):
            round_game([p], shoe)
        self.assertEqual(p.cash, D(100))

    def test_double(self):
        p = Player('P', D(90))
        hands = [Hand(['5', '6'], D(10))]
        shoe = Shoe()
        shoe.cards = ['K']
        with patch('builtins.input', return_value='double'):
            play_hand(p, hands, 0, shoe)
        self.assertEqual(p.cash, D(80))
        self.assertEqual(hands[0].bet, D(20))
        self.assertEqual(total(hands[0].cards), 21)

    def test_split_aces(self):
        p = Player('P', D(90))
        hands = [Hand(['A', 'A'], D(10))]
        shoe = Shoe()
        shoe.cards = ['9', 'K']
        with patch('builtins.input', side_effect=['split']):
            play_hand(p, hands, 0, shoe)
        self.assertEqual(p.cash, D(80))
        self.assertEqual(len(hands), 2)
        self.assertTrue(all(h.split and len(h.cards) == 2 for h in hands))
        settle(p, hands[0], ['10', '8'])
        self.assertEqual(p.cash, D(100))  # Split 21 pays 1:1.

    def test_sit_out_uses_no_cards(self):
        shoe = Shoe()
        before = len(shoe.cards)
        with patch('builtins.input', return_value='0'):
            round_game([Player('P', D(100))], shoe)
        self.assertEqual(len(shoe.cards), before)


if __name__ == '__main__':
    unittest.main()
