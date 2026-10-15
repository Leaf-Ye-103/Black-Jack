import json
from decimal import Decimal as D
import random
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from blackjack import Shoe
from web_blackjack import APIError, Table, make_server


def fixed_shoe(draws=()):
    shoe = Shoe(random.Random(0))
    shoe.cards = ['2'] * 160 + list(reversed(draws))
    return shoe


def configuration(cash='100', players=None, minimum='5', increment='1', maximum='500'):
    return {
        'players': players or [{'name': 'Player', 'cash': cash}],
        'limits': {'minimum': minimum, 'increment': increment, 'maximum': maximum},
    }


class TableTestCase(unittest.TestCase):
    def setUp(self):
        self.table = Table()
        self.addCleanup(self.table.close)

    def create(self, draws=(), **settings):
        self.shoe = fixed_shoe(draws)
        with patch('web_blackjack.Shoe', return_value=self.shoe):
            return self.table.new(configuration(**settings))

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.table.state()
            if state['phase'] == 'error':
                self.fail(state['message'])
            if predicate(state):
                return state
            time.sleep(.01)
        self.fail(f'Table did not reach expected state: {self.table.state()}')

    def prompt(self, phase, previous_id=None):
        return self.wait_for(lambda state: state['prompt'] is not None
                             and state['prompt']['phase'] == phase
                             and state['prompt']['id'] != previous_id)

    def result(self):
        return self.wait_for(lambda state: state['phase'] == 'result'
                             and state['prompt'] is None)

    def answer(self, state, value):
        return self.table.act(state['prompt']['id'], value)


class BrowserTableTests(TableTestCase):
    def test_intermediate_engine_snapshots_are_busy_until_prompt_is_available(self):
        self.create(['10', 'A', '6', '7'])
        transitions = []
        observe = self.table._observe

        def capture(snapshot):
            observe(snapshot)
            public = self.table.state()
            transitions.append((snapshot['phase'], public['phase'], public['prompt']))

        with patch.object(self.table, '_observe', side_effect=capture):
            self.table.start_round()
            self.answer(self.prompt('bet'), '10')
            self.answer(self.prompt('insurance'), '0')
            self.answer(self.prompt('play'), 'stand')
            self.result()

        self.assertEqual({phase for phase, _, _ in transitions},
                         {'bet', 'busy', 'insurance', 'play', 'result'})
        for engine_phase, public_phase, prompt in transitions:
            with self.subTest(engine_phase=engine_phase):
                self.assertEqual(public_phase, 'busy')
                self.assertIsNone(prompt)

    def test_actions_use_engine_and_hide_hole_card_until_settlement(self):
        self.create(['10', '9', '6', '8', '5'])
        self.table.start_round()
        betting = self.prompt('bet')
        self.answer(betting, '10')
        playing = self.prompt('play')
        self.assertEqual(playing['dealer'], {'cards': ['9', None], 'total': None})
        self.assertEqual(playing['players'][0]['hands'][0]['cards'], ['10', '6'])
        self.assertEqual(D(playing['players'][0]['cash']), D(90))
        self.answer(playing, 'hit')
        final = self.result()
        self.assertEqual(final['dealer'], {'cards': ['9', '8'], 'total': 17})
        self.assertEqual(final['players'][0]['hands'][0]['total'], 21)
        self.assertEqual(final['players'][0]['hands'][0]['outcome'], 'win')
        self.assertEqual(D(final['players'][0]['cash']), D(110))
        self.assertIsNone(final['current_player'])

    def test_invalid_wagers_preserve_pending_decision_cash_and_shoe(self):
        self.create(['K', '10', 'Q', '7'], cash='1000', minimum='3', increment='.50')
        self.table.start_round()
        betting = self.prompt('bet')
        self.assertEqual(D(betting['prompt']['max_stake']), D(500))
        cards = self.shoe.cards.copy()
        for value in ('2.50', '3.25', '500.50', '1001', 'NaN', '1e1', 10, True):
            with self.subTest(wager=value), self.assertRaises(APIError):
                self.answer(betting, value)
            self.assertEqual(self.table.state(), betting)
            self.assertEqual(self.shoe.cards, cards)
        self.answer(betting, '3.50')
        self.answer(self.prompt('play'), 'stand')
        self.assertEqual(D(self.result()['players'][0]['cash']), D('1003.50'))

    def test_double_clicks_stale_ids_and_illegal_actions_do_not_advance(self):
        self.create(['5', '10', '6', '7', 'K'])
        self.table.start_round()
        betting = self.prompt('bet')
        self.answer(betting, '10')
        playing = self.prompt('play')
        for prompt_id in (betting['prompt']['id'], True, None):
            with self.subTest(prompt_id=prompt_id), self.assertRaises(APIError) as error:
                self.table.act(prompt_id, 'stand')
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.table.state(), playing)
        with self.assertRaises(APIError):
            self.answer(playing, 'split')
        self.assertEqual(self.table.state(), playing)
        self.assertIn('double', playing['prompt']['actions'])
        self.answer(playing, 'double')
        with self.assertRaises(APIError) as error:
            self.answer(playing, 'double')
        self.assertEqual(error.exception.status, 409)
        final = self.result()
        self.assertEqual(D(final['players'][0]['cash']), D(120))
        self.assertEqual(D(final['players'][0]['hands'][0]['bet']), D(20))

    def test_opening_cap_allows_additional_double_wager(self):
        self.create(['5', '10', '6', '7', 'K'], cash='1500')
        self.table.start_round()
        self.answer(self.prompt('bet'), '500')
        self.answer(self.prompt('play'), 'double')
        final = self.result()
        self.assertEqual(D(final['players'][0]['cash']), D(2500))
        self.assertEqual(D(final['players'][0]['hands'][0]['bet']), D(1000))

    def test_insurance_uses_half_bet_in_cents_without_opening_minimum(self):
        self.create(['10', 'A', '9', 'K'])
        self.table.start_round()
        self.answer(self.prompt('bet'), '5')
        insurance = self.prompt('insurance')
        self.assertEqual(D(insurance['prompt']['max_stake']), D('2.50'))
        self.assertEqual(insurance['dealer'], {'cards': ['A', None], 'total': None})
        with self.assertRaises(APIError):
            self.answer(insurance, '2.51')
        self.assertEqual(self.table.state(), insurance)
        self.answer(insurance, '2.50')
        final = self.result()
        self.assertEqual(D(final['players'][0]['cash']), D(100))
        self.assertEqual(final['dealer']['cards'], ['A', 'K'])
        self.assertEqual(final['players'][0]['hands'][0]['outcome'], 'dealer blackjack: loss')

    def test_multiple_seats_sit_out_and_split_in_the_same_round(self):
        players = [{'name': name, 'cash': '100'} for name in ('Out', 'Split', 'Stand')]
        self.create(['8', 'K', '10', '8', 'Q', '7', 'K', '9'], players=players)
        self.table.start_round()
        for seat, wager in enumerate(('0', '10', '20')):
            betting = self.prompt('bet')
            self.assertEqual(betting['current_player'], seat)
            self.answer(betting, wager)
        playing = self.prompt('play')
        self.assertEqual(playing['current_player'], 1)
        self.answer(playing, 'split')
        first = self.prompt('play', playing['prompt']['id'])
        self.assertEqual(first['current_hand'], 0)
        self.assertEqual([hand['cards'] for hand in first['players'][1]['hands']],
                         [['8', 'K'], ['8', '9']])
        self.answer(first, 'stand')
        second = self.prompt('play', first['prompt']['id'])
        self.assertEqual((second['current_player'], second['current_hand']), (1, 1))
        self.answer(second, 'stand')
        third = self.prompt('play', second['prompt']['id'])
        self.assertEqual(third['current_player'], 2)
        self.answer(third, 'stand')
        final = self.result()
        self.assertEqual([D(p['cash']) for p in final['players']], [D(100), D(110), D(120)])
        self.assertEqual(final['players'][0]['hands'], [])
        self.assertEqual([h['outcome'] for h in final['players'][1]['hands']], ['win', 'push'])

    def test_players_below_minimum_skip_and_everyone_sitting_out_uses_no_cards(self):
        self.create(['A', 'K'], players=[{'name': 'Short', 'cash': '2.99'},
                                       {'name': 'Out', 'cash': '100'},
                                       {'name': 'Empty', 'cash': '0'}])
        cards = self.shoe.cards.copy()
        self.table.start_round()
        betting = self.prompt('bet')
        self.assertEqual(betting['current_player'], 1)
        self.answer(betting, '0')
        final = self.result()
        self.assertEqual(self.shoe.cards, cards)
        self.assertEqual(final['dealer'], {'cards': [], 'total': None})
        self.assertEqual([D(p['cash']) for p in final['players']], [D('2.99'), D(100), D(0)])
        self.assertTrue(all(not player['hands'] for player in final['players']))

    def test_config_limits_and_bankroll_are_validated_before_replacing_table(self):
        original = self.create()
        invalid = [
            {'players': []}, {'players': [{'cash': '1'}] * 8},
            configuration(minimum='3.01'), configuration(increment='0'),
            configuration(minimum='5', maximum='3'), configuration(cash='NaN'),
            configuration(cash=100), configuration(cash='-1'),
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(APIError):
                self.table.new(config)
            self.assertEqual(self.table.state(), original)

    def test_table_or_round_cannot_be_replaced_while_waiting_for_input(self):
        self.create()
        self.table.start_round()
        betting = self.prompt('bet')
        for operation in (self.table.start_round,
                          lambda: self.table.new(configuration(cash='500'))):
            with self.assertRaises(APIError) as error:
                operation()
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.table.state(), betting)

    def test_next_round_carries_cash_and_shoe_then_settled_table_can_reset(self):
        self.create(['K', '10', 'Q', '7', '10', '9', '6', '8'])
        self.table.start_round()
        first_bet = self.prompt('bet')
        self.answer(first_bet, '10')
        self.answer(self.prompt('play'), 'stand')
        self.assertEqual(D(self.result()['players'][0]['cash']), D(110))

        self.table.start_round()
        second_bet = self.prompt('bet')
        self.assertEqual(second_bet['round'], 2)
        self.assertEqual(D(second_bet['players'][0]['cash']), D(110))
        self.answer(second_bet, '10')
        playing = self.prompt('play')
        self.assertEqual(playing['players'][0]['hands'][0]['cards'], ['10', '6'])
        self.answer(playing, 'surrender')
        self.assertEqual(D(self.result()['players'][0]['cash']), D(105))

        reset = self.table.new(configuration(cash='200'))
        self.assertEqual(reset['round'], 0)
        self.assertEqual(reset['players'][0]['hands'], [])
        self.assertEqual(D(reset['players'][0]['cash']), D(200))
        self.table.start_round()
        new_bet = self.prompt('bet')
        with self.assertRaises(APIError) as error:
            self.answer(first_bet, '10')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.table.state(), new_bet)

    def test_returned_state_is_detached_and_close_releases_pending_worker(self):
        self.create()
        self.table.start_round()
        betting = self.prompt('bet')
        betting['players'][0]['cash'] = '999999'
        betting['prompt']['actions'].append('cheat')
        state = self.table.state()
        self.assertEqual(D(state['players'][0]['cash']), D(100))
        self.assertNotIn('cheat', state['prompt']['actions'])
        worker = self.table._thread
        self.assertTrue(worker.is_alive())
        self.table.close()
        self.assertFalse(worker.is_alive())
        with self.assertRaises(APIError):
            self.table.start_round()


class BrowserHTTPTests(TableTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(table=self.table)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload=None, raw=None, headers=None):
        request_headers = {'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else raw
        request = Request(self.base + path, data=body, headers=request_headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_assets_state_and_http_game_flow(self):
        for asset, kind in (('/', 'text/html'), ('/style.css', 'text/css'),
                            ('/app.js', 'text/javascript')):
            with self.subTest(asset=asset):
                status, headers, body = self.request(asset)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), kind)
                self.assertTrue(body)
        shoe = fixed_shoe(['A', '9', 'K', '8'])
        with patch('web_blackjack.Shoe', return_value=shoe):
            status, headers, body = self.request('/api/new', configuration())
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), 'application/json')
        self.assertEqual(json.loads(body)['players'][0]['name'], 'Player')
        self.assertEqual(self.request('/api/round', {})[0], 200)
        betting = self.prompt('bet')
        status, _, _ = self.request('/api/action', {'prompt_id': betting['prompt']['id'], 'value': '10'})
        self.assertEqual(status, 200)
        self.result()
        status, headers, body = self.request('/api/state')
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(D(state['players'][0]['cash']), D(115))
        self.assertEqual(state['players'][0]['hands'][0]['outcome'], 'blackjack! pays 3:2')

    def test_invalid_http_input_is_json_and_does_not_create_a_table(self):
        requests = [
            ({'raw': b'{broken'}, 400),
            ({'raw': b'[]'}, 400),
            ({'raw': b'{}', 'headers': {'Content-Type': 'text/plain'}}, 415),
            ({'payload': configuration(), 'headers': {'Origin': 'https://unrelated.example'}}, 403),
            ({'raw': b' ' * 20000}, 413),
        ]
        for arguments, expected_status in requests:
            with self.subTest(arguments={k: v if k != 'raw' else len(v) for k, v in arguments.items()}):
                status, headers, body = self.request('/api/new', **arguments)
                self.assertEqual(status, expected_status)
                self.assertEqual(headers.get_content_type(), 'application/json')
                self.assertTrue(json.loads(body)['error'])
                self.assertEqual(self.table.state()['phase'], 'setup')
        # The exact same origin as the local table is accepted.
        status, _, _ = self.request('/api/new', configuration(), headers={'Origin': self.base})
        self.assertEqual(status, 200)

    def test_unknown_paths_and_source_traversal_do_not_expose_files(self):
        for path in ('/missing', '/../blackjack.py', '/%2e%2e/blackjack.py',
                     '/blackjack.py', '/web/../web_blackjack.py'):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers.get_content_type(), 'application/json')
                self.assertTrue(json.loads(body)['error'])
        status, headers, body = self.request('/api/missing', {})
        self.assertEqual(status, 404)
        self.assertEqual(headers.get_content_type(), 'application/json')
        self.assertTrue(json.loads(body)['error'])


if __name__ == '__main__':
    unittest.main()
