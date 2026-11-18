"""Mock-only Ollama Cloud transport tests; never read a real key or use network."""

import json
import unittest
from unittest.mock import patch

import httpx

from ipo_monitor import CollectionError
from ipo_monitor.ollama import ENDPOINT, INSTRUCTIONS, OllamaAnalyzer

SOURCE = {'stock': '가상기업', 'content': {'text': '가상기업 청약에 참여하겠습니다.'}}
RESULT = {'opinion': '참여', 'assessment': 'positive',
          'evidence': '가상기업 청약에 참여하겠습니다.', 'stage': 'planned', 'reason': '작성자 참여 의사'}
SECRET = 'test-secret-never-log'


def analyzer(max_calls=2, model='test-model:cloud'):
    return OllamaAnalyzer(model, True, max_calls, SECRET)


def response(payload=None, status=200):
    if payload is None:
        payload = {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(RESULT)}}
    return httpx.Response(status, json=payload, request=httpx.Request('POST', ENDPOINT))


class OllamaTests(unittest.TestCase):
    def test_constructor_requires_explicit_approval_model_key_and_bounded_calls(self):
        bad = [('', True, 2, SECRET), ('model\nsecret', True, 2, SECRET),
               ('model', False, 2, SECRET), ('model', 1, 2, SECRET),
               ('model', True, 0, SECRET), ('model', True, True, SECRET),
               ('model', True, 601, SECRET), ('model', True, 1, ''),
               ('model', True, 1, 'secret\nheader'), ('model', True, 1, ' 비밀 ')]
        with patch('ipo_monitor.ollama.httpx.post') as post:
            for args in bad:
                with self.subTest(args=args), self.assertRaises(CollectionError):
                    OllamaAnalyzer(*args)
            post.assert_not_called()

    def test_exact_cloud_request_and_grounded_response(self):
        backend = analyzer()
        with patch('ipo_monitor.ollama.httpx.post', return_value=response()) as post:
            self.assertEqual(backend.analyze('input-hash', SOURCE), RESULT)
        args, kwargs = post.call_args
        self.assertEqual(args, (ENDPOINT,))
        self.assertEqual(kwargs['headers'], {'Authorization': f'Bearer {SECRET}'})
        self.assertFalse(kwargs['follow_redirects']); self.assertFalse(kwargs['trust_env'])
        self.assertEqual(kwargs['timeout'], 45)
        body = kwargs['json']
        self.assertEqual(body['model'], 'test-model:cloud')
        self.assertIs(body['stream'], False)
        self.assertEqual(body['options'], {'temperature': 0, 'num_predict': 1200})
        self.assertEqual(body['messages'][0], {'role': 'system', 'content': INSTRUCTIONS})
        sent=json.loads(body['messages'][1]['content'])
        self.assertEqual(sent['stock'],SOURCE['stock'])
        self.assertEqual(sent['content'],SOURCE['content'])
        self.assertIn(RESULT['evidence'],sent['evidence_choices'])
        self.assertNotIn('format', body)
        self.assertIs(body['think'],False)
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertFalse(backend.is_test); self.assertEqual(backend.label, 'Ollama')
        self.assertEqual(backend.origin, 'ollama')
        self.assertEqual(backend.calls, 1)

    def test_explicit_600_request_budget_is_supported_without_dispatch(self):
        client=analyzer(max_calls=600)
        self.assertEqual(client.max_calls,600)
        self.assertEqual(client.calls,0)

    def test_cache_identity_is_provider_model_and_prompt_bound_not_key_or_input(self):
        first = analyzer()
        other_key = OllamaAnalyzer('test-model:cloud', True, 2, 'another-test-secret')
        self.assertEqual(first.key('one'), first.key('two'))
        self.assertEqual(first.key('one'), other_key.key('one'))
        self.assertNotEqual(first.key('one'), analyzer(model='different-model').key('one'))
        self.assertTrue(first.key('one').startswith('ollama:'))
        self.assertNotIn(SECRET, first.key('one')); self.assertNotIn(SECRET, repr(first))
        first._settings = {**first._settings, 'instructions': 'changed prompt'}
        self.assertNotEqual(first.key('one'), other_key.key('one'))

    def test_successes_and_failures_both_consume_budget_without_retries(self):
        backend = analyzer(max_calls=2)
        with patch('ipo_monitor.ollama.httpx.post', side_effect=[response(status=429), response()]) as post:
            with self.assertRaisesRegex(CollectionError, '^ollama_http_429$'):
                backend.analyze('hash', SOURCE)
            self.assertEqual(backend.analyze('hash', SOURCE), RESULT)
            with self.assertRaisesRegex(CollectionError, '^ollama_call_limit_reached$'):
                backend.analyze('hash', SOURCE)
            self.assertEqual(post.call_count, 2)
        self.assertEqual(backend.calls, 2)

    def test_network_error_and_http_error_are_static_no_secret_leak(self):
        for failure in (httpx.ConnectError(SECRET), httpx.ReadTimeout(SECRET)):
            backend = analyzer()
            with self.subTest(failure=type(failure)), patch('ipo_monitor.ollama.httpx.post', side_effect=failure), self.assertRaises(CollectionError) as caught:
                backend.analyze('hash', SOURCE)
            self.assertEqual(str(caught.exception), 'ollama_network_error')
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(backend.calls, 1)
        backend = analyzer()
        with patch('ipo_monitor.ollama.httpx.post', return_value=response({'error': SECRET}, status=503)), self.assertRaisesRegex(CollectionError, '^ollama_http_503$'):
            backend.analyze('hash', SOURCE)

    def test_redirect_is_not_followed_or_authorization_forwarded(self):
        redirect = httpx.Response(302, headers={'Location': 'https://evil.invalid/'}, request=httpx.Request('POST', ENDPOINT))
        with patch('ipo_monitor.ollama.httpx.post', return_value=redirect) as post, self.assertRaisesRegex(CollectionError, '^ollama_redirect_blocked$'):
            analyzer().analyze('hash', SOURCE)
        post.assert_called_once()
        self.assertFalse(post.call_args.kwargs['follow_redirects'])

    def test_invalid_or_oversize_input_does_not_consume_call(self):
        backend = analyzer()
        for source in (None, {}, {'stock': '', 'content': {'text': 'x'}},
                       {'stock': '가상기업', 'content': {'text': ''}},
                       {'stock': '가상기업', 'content': {'text': 'x' * 100_001}}):
            with self.subTest(source=str(source)[:50]), patch('ipo_monitor.ollama.httpx.post') as post, self.assertRaises(CollectionError):
                backend.analyze('hash', source)
            post.assert_not_called()
        self.assertEqual(backend.calls, 0)

    def test_incomplete_error_or_tool_response_is_not_success(self):
        payloads = [
            {'done': False, 'message': {'content': json.dumps(RESULT)}},
            {'done': True, 'done_reason': 'length', 'message': {'content': json.dumps(RESULT)}},
            {'done': True, 'error': SECRET},
            {'done': True, 'done_reason': 'unknown', 'message': {'content': json.dumps(RESULT)}},
            {'done': True, 'message': {'content': json.dumps(RESULT), 'refusal': SECRET}},
            {'done': True, 'message': {'content': json.dumps(RESULT), 'tool_calls': [{'function': 'unsafe'}]}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), patch('ipo_monitor.ollama.httpx.post', return_value=response(payload)), self.assertRaises(CollectionError) as caught:
                analyzer().analyze('hash', SOURCE)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_malformed_outer_response_or_non_json_text_is_safe_error(self):
        payloads = [[], 'text', {'done': True}, {'done': True, 'message': []},
                    {'done': True, 'message': {'content': ''}},
                    {'done': True, 'message': {'content': SECRET}},
                    {'done': True, 'message': {'content': '```json\n{}\n```'}},
                    {'done': True, 'message': {'content': '{"opinion":"참여","opinion":"미참여"}'}},
                    {'done': True, 'message': {'content': 123}}]
        for payload in payloads:
            with self.subTest(payload=payload), patch('ipo_monitor.ollama.httpx.post', return_value=response(payload)), self.assertRaises(CollectionError) as caught:
                analyzer().analyze('hash', SOURCE)
            self.assertNotIn(SECRET, str(caught.exception))
        invalid = httpx.Response(200, content=SECRET.encode(), request=httpx.Request('POST', ENDPOINT))
        with patch('ipo_monitor.ollama.httpx.post', return_value=invalid), self.assertRaisesRegex(CollectionError, '^ollama_response_invalid$'):
            analyzer().analyze('hash', SOURCE)

    def test_local_schema_and_quote_validation_are_mandatory(self):
        cases = [{**RESULT, 'evidence': '허위 근거'}, {**RESULT, 'extra': 'field'},
                 {**RESULT, 'opinion': 'invalid'}, {**RESULT, 'evidence': ' '}]
        for result in cases:
            payload = {'done': True, 'message': {'content': json.dumps(result)}}
            with self.subTest(result=result), patch('ipo_monitor.ollama.httpx.post', return_value=response(payload)), self.assertRaises(CollectionError):
                analyzer().analyze('hash', SOURCE)


class OllamaUsageTests(unittest.TestCase):
    def test_only_safe_usage_metadata_is_retained(self):
        backend=analyzer()
        payload={'done':True,'done_reason':'stop','prompt_eval_count':123,'eval_count':45,
                 'message':{'content':json.dumps(RESULT)}}
        with patch('ipo_monitor.ollama.httpx.post',return_value=response(payload)):
            backend.analyze('test-hash',SOURCE)
        self.assertEqual(backend.usage[0]['input_tokens'],123)
        self.assertEqual(backend.usage[0]['output_tokens'],45)
        self.assertFalse(backend.usage[0]['thinking_used'])
        self.assertNotIn(SECRET,json.dumps(backend.usage))
        self.assertNotIn('content',backend.usage[0])
