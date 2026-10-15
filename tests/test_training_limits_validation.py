from decimal import Decimal as D
import unittest

from train_blackjack import Settings, session, sit_out, train


class TrainingLimitsValidationTests(unittest.TestCase):
    def test_training_rejects_bankroll_below_minimum(self):
        settings = Settings(bankroll='4.99', min_bet='5', bet_increment='1', max_bet='500')
        with self.assertRaisesRegex(ValueError, 'bankroll.*minimum opening bet'):
            train(settings, episodes=1, seed=0)

        # Finished bankrolls below the minimum still remain valid for replay.
        self.assertEqual(session(settings, sit_out, seed=0), (D('4.99'), 0))

    def test_training_accepts_bankroll_at_minimum(self):
        settings = Settings(bankroll='5', rounds=1,
                            min_bet='5', bet_increment='1', max_bet='500')
        agent = train(settings, episodes=3, seed=0)
        self.assertTrue(agent.table)


if __name__ == '__main__':
    unittest.main()
