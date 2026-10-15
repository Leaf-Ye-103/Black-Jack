"""Exercise the shared game's public entry points in fresh Python processes."""

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / 'blackjack.py'


class GameEntrypointTests(unittest.TestCase):
    def test_import_is_quiet_and_training_uses_the_same_engine(self):
        result = subprocess.run(
            [sys.executable, '-c', '''
import builtins

def unexpected(*args, **kwargs):
    raise AssertionError('Import must not prompt for input.')

builtins.input = unexpected
import blackjack
import train_blackjack
assert train_blackjack.round_game is blackjack.round_game
assert train_blackjack.Shoe is blackjack.Shoe
'''],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')

    def test_explicit_and_legacy_terminal_commands_finish_a_round(self):
        for mode in ([], ['play']):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, str(GAME), *mode, '--min-bet', '3',
                     '--bet-increment', '.50', '--max-bet', '9999'],
                    input='1\nTester\n10\n0\nn\n', capture_output=True,
                    text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('minimum $3.00, increments $0.50, maximum $9,999.00',
                              result.stdout)
                self.assertIn('Everyone sat out. No cards dealt.', result.stdout)
                self.assertIn('Final balances:\nTester: $10.00', result.stdout)


if __name__ == '__main__':
    unittest.main()
