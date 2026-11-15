"""Offline tests for versioned semantic schema and opt-in API transport."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from ipo_monitor.extraction import ExtractionError, digest
from ipo_monitor.semantic import AIBackend, FixtureBackend, SCHEMA, needs_body, validate


SOURCE = {"title": "가상기업 공모주", "summary": "가상기업 청약에 참여하겠습니다.",
          "link": "https://example.invalid/post", "published_raw": "", "published_at": None}
RESULT = {"category": "ipo", "candidates": [{"name": "가상기업", "opinion": "참여",
          "evidence": "가상기업 청약에 참여하겠습니다.", "basis": "author_opinion", "stage": "planned"}]}


class SemanticTests(unittest.TestCase):
    def test_grounded_participation_needs_no_body(self):
        self.assertEqual(validate(RESULT, SOURCE), RESULT)
        self.assertFalse(needs_body(RESULT))

    def test_unknown_and_uncertain_trigger_body(self):
        result = copy.deepcopy(RESULT)
        result['candidates'][0].update(opinion='판단 불가', basis='insufficient', stage='unknown')
        self.assertTrue(needs_body(validate(result, SOURCE)))
        self.assertTrue(needs_body(validate({'category': 'uncertain', 'candidates': []}, SOURCE)))

    def test_conditional_remains_distinct_and_needs_no_body(self):
        result = copy.deepcopy(RESULT)
        result['candidates'][0]['opinion'] = '조건부'
        self.assertFalse(needs_body(validate(result, SOURCE)))

    def test_property_and_other_have_no_candidates_or_fallback(self):
        for category in ('property', 'other'):
            self.assertFalse(needs_body(validate({'category': category, 'candidates': []}, SOURCE)))
            with self.assertRaises(ExtractionError):
                validate({**RESULT, 'category': category}, SOURCE)

    def test_body_only_evidence_is_accepted(self):
        source = {**SOURCE, 'title': '청약 글', 'summary': '', 'body': SOURCE['summary']}
        self.assertEqual(validate(RESULT, source), RESULT)

    def test_unquoted_or_invented_names_are_rejected(self):
        for field, value in [('name', '없는기업'), ('evidence', '지어낸 문장')]:
            result = copy.deepcopy(RESULT)
            result['candidates'][0][field] = value
            with self.assertRaisesRegex(ExtractionError, 'evidence_not_in_source'):
                validate(result, SOURCE)

    def test_mention_cannot_claim_participation_or_application(self):
        for changes in ({'basis': 'mention'}, {'basis': 'mention', 'opinion': '판단 불가', 'stage': 'applied'}):
            result = copy.deepcopy(RESULT)
            result['candidates'][0].update(changes)
            with self.assertRaisesRegex(ExtractionError, 'unsupported_opinion'):
                validate(result, SOURCE)

    def test_malformed_schema_rejected_without_crash(self):
        cases = [None, [], {}, {'category': [], 'candidates': []},
                 {'category': 'ipo', 'candidates': []}, {'category': 'ipo', 'candidates': [None]}]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ExtractionError):
                validate(result, SOURCE)

    def test_extra_fields_duplicate_names_and_invalid_enum_rejected(self):
        cases = []
        extra = copy.deepcopy(RESULT)
        extra['candidates'][0]['unexpected'] = 'value'
        cases.append(extra)
        duplicate = copy.deepcopy(RESULT)
        duplicate['candidates'].append(copy.deepcopy(duplicate['candidates'][0]))
        cases.append(duplicate)
        enum = copy.deepcopy(RESULT)
        enum['candidates'][0]['opinion'] = '패스'
        cases.append(enum)
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ExtractionError):
                validate(result, SOURCE)

    def test_fixture_bound_to_input_and_unrelated_addition_keeps_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixtures.json'
            responses = {digest(SOURCE): RESULT}
            path.write_text(json.dumps(responses), encoding='utf-8')
            first = FixtureBackend(path)
            self.assertEqual(first.extract(SOURCE), RESULT)
            responses['unrelated'] = {'category': 'other', 'candidates': []}
            path.write_text(json.dumps(responses), encoding='utf-8')
            self.assertEqual(first.cache_key(SOURCE), FixtureBackend(path).cache_key(SOURCE))
            with self.assertRaisesRegex(ExtractionError, 'input_mismatch'):
                first.extract({**SOURCE, 'summary': 'changed'})


class SemanticApiTests(unittest.TestCase):
    def backend(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'never-log-key'}):
            return AIBackend('configured-model', True)

    def response(self, payload, status=200):
        return httpx.Response(status, json=payload,
                              request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))

    def completed(self, result=RESULT):
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(result)}]}]}

    def test_requires_explicit_opt_in_and_key(self):
        with patch('ipo_monitor.semantic.httpx.post') as post:
            with self.assertRaisesRegex(ExtractionError, 'api_not_authorized'):
                AIBackend('model', False)
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ExtractionError, 'api_key_missing'):
                AIBackend('model', True)
            post.assert_not_called()

    def test_structured_request_and_validated_response(self):
        with patch('ipo_monitor.semantic.httpx.post', return_value=self.response(self.completed())) as post:
            self.assertEqual(self.backend().extract(SOURCE), RESULT)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['model'], 'configured-model')
        self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['schema'], SCHEMA)
        self.assertTrue(body['text']['format']['strict'])
        self.assertNotIn('never-log-key', json.dumps(body))

    def test_refusal_incomplete_and_invalid_outer_shapes_are_errors(self):
        cases = [({'status': 'incomplete'}, 'api_incomplete'),
                 ({'status': 'completed', 'output': [{'type': 'message', 'content': [
                     {'type': 'refusal', 'refusal': 'never-log-key'}]}]}, 'api_refusal'),
                 (None, 'api_response_invalid'),
                 ({'status': 'completed', 'output': None}, 'api_response_invalid')]
        for payload, code in cases:
            with self.subTest(payload=payload), patch('ipo_monitor.semantic.httpx.post', return_value=self.response(payload)):
                with self.assertRaisesRegex(ExtractionError, code) as error:
                    self.backend().extract(SOURCE)
                self.assertNotIn('never-log-key', str(error.exception))

    def test_schema_invalid_completed_response_is_not_success(self):
        with patch('ipo_monitor.semantic.httpx.post', return_value=self.response(self.completed({'bad': 'data'}))):
            with self.assertRaisesRegex(ExtractionError, 'schema_invalid'):
                self.backend().extract(SOURCE)

    def test_http_and_network_errors_are_safe_and_not_retried(self):
        for response, side_effect, code in [(self.response({'secret': 'never-log-key'}, 429), None, 'api_http_429'),
                                           (None, httpx.ReadTimeout('never-log-key'), 'api_network_error')]:
            with patch('ipo_monitor.semantic.httpx.post', return_value=response, side_effect=side_effect) as post:
                with self.assertRaisesRegex(ExtractionError, code) as error:
                    self.backend().extract(SOURCE)
                self.assertNotIn('never-log-key', str(error.exception))
                self.assertEqual(post.call_count, 1)
