import unittest
from pathlib import Path
from cbor_codec import dumps, loads

FIXTURES = Path(__file__).parent / 'fixtures'

class NativeInteroperability(unittest.TestCase):
    def test_original_dll_attach_fixture(self):
        raw = (FIXTURES / 'attach.cbor').read_bytes()
        self.assertEqual(raw.hex(), 'a166417474616368a1637069641a00067932')
        self.assertEqual(loads(raw), {'Attach': {'pid': 424242}})
        self.assertEqual(dumps(loads(raw)), raw)

    def test_all_original_dll_log_levels(self):
        for number, level in enumerate(('Error', 'Warn', 'Info', 'Debug', 'Trace')):
            with self.subTest(level=level):
                raw = (FIXTURES / f'log-{number}.cbor').read_bytes()
                expected = {'Log': {'pid': 424242, 'tid': 12345,
                    'timestamp': 123456789, 'level': level, 'text': 'IPC fixture only'}}
                self.assertEqual(loads(raw), expected)
                self.assertEqual(dumps(expected), raw)

    def test_handshake_accepted_by_original_dll_decoder(self):
        raw = (FIXTURES / 'handshake-synthetic.cbor').read_bytes()
        import json
        recorded = json.loads((FIXTURES / 'codec-results.json').read_text())
        self.assertTrue(recorded['native_decode_accepted'])
        self.assertEqual(loads(raw), recorded['handshake'])
        self.assertEqual(dumps(recorded['handshake']), raw)
        for case in recorded['validation_cases']:
            if case['case'].startswith('missing'):
                self.assertFalse(case['accepted'])

    def test_truncated_native_event_rejected(self):
        raw = (FIXTURES / 'attach.cbor').read_bytes()
        for size in range(len(raw)):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    loads(raw[:size])

    def test_no_silent_trailing_bytes(self):
        with self.assertRaises(ValueError):
            loads((FIXTURES / 'attach.cbor').read_bytes() + b'\x00')

if __name__ == '__main__':
    unittest.main()
