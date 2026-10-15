"""Serve the local multiplayer blackjack table at http://127.0.0.1:8000.

The browser supplies human decisions through the same policy interface used by
the trainer. All dealing, legal actions, and settlement stay in blackjack.py.
"""

import argparse
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from blackjack import BetLimits, CASINO_LIMITS, Player, Shoe, round_game


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', help='Bind address (default: 127.0.0.1).')
    parser.add_argument('--port', default=8000, type=int, help='Port (default: 8000).')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    server = make_server(args.host, args.port)
    print(f'Blackjack table: http://{args.host}:{server.server_address[1]}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nClosing the table.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
