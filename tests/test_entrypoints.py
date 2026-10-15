"""Exercise the shared game's public entry points in fresh Python processes."""

import json
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / 'blackjack.py'


class GameEntrypointTests(unittest.TestCase):
    def test_import_is_quiet_and_training_uses_the_same_engine(self):
        result = subprocess.run(
            [sys.executable, '-c', '''
import builtins
import socket

def unexpected(*args, **kwargs):
    raise AssertionError('Import must not prompt or bind a server.')

builtins.input = unexpected
socket.socket.bind = unexpected
import blackjack
import train_blackjack
assert train_blackjack.round_game is blackjack.round_game
assert train_blackjack.Shoe is blackjack.Shoe
assert callable(blackjack.make_server)
assert callable(blackjack.Table)
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

    def test_browser_command_serves_assets_and_stops_with_a_pending_turn(self):
        # An unrelated working directory also checks that assets resolve next to
        # blackjack.py, rather than depending on the launch directory.
        with tempfile.TemporaryDirectory() as folder:
            process = subprocess.Popen(
                [sys.executable, '-u', str(GAME), 'web', '--host', '127.0.0.1',
                 '--port', '0'],
                cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            startup = queue.Queue()
            reader = threading.Thread(
                target=lambda: startup.put(process.stdout.readline()), daemon=True,
            )
            reader.start()
            try:
                line = startup.get(timeout=10).strip()
                self.assertTrue(line.startswith('Blackjack table: http://127.0.0.1:'),
                                line)
                base = line.removeprefix('Blackjack table: ')
                self.assertNotEqual(base.rsplit(':', 1)[1], '0')
                for path, content_type in (('/', 'text/html'), ('/style.css', 'text/css'),
                                           ('/app.js', 'text/javascript'),
                                           ('/favicon.svg', 'image/svg+xml')):
                    with self.subTest(path=path), urlopen(base + path, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers.get_content_type(), content_type)
                        self.assertTrue(response.read())

                with urlopen(base + '/api/state', timeout=3) as response:
                    self.assertEqual(json.load(response)['phase'], 'setup')

                config = {'players': [{'name': 'Tester', 'cash': '100'}],
                          'limits': {'minimum': '3', 'increment': '.50', 'maximum': '9999'}}
                for path, payload in (('/api/new', config), ('/api/round', {})):
                    request = Request(base + path, json.dumps(payload).encode(),
                                      {'Content-Type': 'application/json'})
                    with urlopen(request, timeout=3) as response:
                        self.assertEqual(response.status, 200)

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with urlopen(base + '/api/state', timeout=3) as response:
                        state = json.load(response)
                    if state['prompt'] is not None:
                        break
                    time.sleep(.01)
                self.assertIsNotNone(state['prompt'])
                self.assertEqual(state['prompt']['phase'], 'bet')
                process.send_signal(signal.SIGINT)
                output, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, errors)
                self.assertIn('Closing the table.', output)
                self.assertEqual(errors, '')
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
                reader.join(timeout=1)


if __name__ == '__main__':
    unittest.main()
