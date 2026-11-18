import unittest

from ipo_monitor import CollectionError
from ipo_monitor.assessment import validate_response
from ipo_monitor.event_validation import validate_event_response
from ipo_monitor.ollama import evidence_choices


def source(text, stock='가상기업', aliases=None):
    return {'version': 'stock-evidence-v1', 'stock': stock,
            'aliases': [stock] if aliases is None else aliases,
            'source_id': 'sample-blog', 'source_type': 'rss', 'creator': '팽',
            'url': 'https://blog.naver.com/hycj1025/123456789', 'title': '가상기업 공모주 검토',
            'published_raw': '', 'published_at': None,
            'content': {'text': text, 'segments': [], 'events': [], 'ratings': [], 'table_rows': []}}


def response(evidence, reason='청약 참여 의견을 밝혔습니다.', opinion='참여', stage='planned'):
    return {'opinion': opinion, 'assessment': 'positive', 'evidence': evidence,
            'stage': stage, 'reason': reason}


def reject(test, payload, origin, code):
    with test.assertRaises(CollectionError) as caught:
        validate_event_response(payload, origin)
    test.assertEqual(str(caught.exception), code)


class EventValidationTest(unittest.TestCase):
    def test_information_title_is_not_a_participation_declaration(self):
        text = '가상기업 청약 정보입니다.'
        reject(self, response(text, '청약에 참여한다고 밝혔습니다.', stage='unknown'),
               source(text), 'event_opinion_ungrounded')

    def test_conditional_intent_cannot_be_promoted_to_unconditional_participation(self):
        text = '가상기업은 경쟁률이 낮으면 참여하겠습니다.'
        reject(self, response(text), source(text), 'event_opinion_ungrounded')

    def test_other_stock_amount_cannot_ground_target_stock_summary(self):
        text = '가상기업은 청약에 참여하겠습니다.\n다른기업의 공모가는 50,000원입니다.'
        reject(self, response('가상기업은 청약에 참여하겠습니다.', '공모가는 50000원입니다.'),
               source(text), 'event_reason_number_ungrounded')

    def test_unknown_opinion_cannot_hide_fabricated_participation_in_reason(self):
        text = '가상기업 청약 정보입니다.'
        reject(self, response(text, '청약에 참여한다고 밝혔습니다.', opinion='판단 불가', stage='unknown'),
               source(text), 'event_opinion_ungrounded')

    def test_explicit_pass_remains_nonparticipation(self):
        text = '가상기업 청약은 패스하겠습니다.'
        payload = response(text, '청약을 패스하겠다고 밝혔습니다.', opinion='미참여')
        self.assertIs(validate_event_response(payload, source(text)), payload)

    def test_invalid_calendar_date_cannot_evade_number_validation(self):
        text = '가상기업 청약 정보입니다.'
        reject(self, response(text, '13월 40일 청약입니다.', opinion='판단 불가', stage='unknown'),
               source(text), 'event_reason_date_ungrounded')

    def test_planned_participation_and_normalized_numbers_pass(self):
        text = ('가상기업 공모가 40,000원, 청약일은 2026-11-20입니다.\n'
                '가상기업은 청약에 참여하겠습니다.')
        origin = source(text)
        payload = response('가상기업은 청약에 참여하겠습니다.',
                           '공모가 40000원이며 11월 20일 청약에 참여할 예정입니다.')
        self.assertIs(validate_event_response(payload, origin), payload)

    def test_numbers_with_commas_are_grounded_as_whole_tokens(self):
        text = '가상기업은 경쟁률 1200대 1, 수익률 45%를 언급했습니다.'
        payload = response(text, '경쟁률 1,200대 1과 45%를 언급했습니다.', opinion='판단 불가', stage='unknown')
        self.assertIs(validate_event_response(payload, source(text)), payload)

    def test_decimal_claims_are_matched_as_whole_percent_tokens(self):
        text = '가상기업은 수익률 4.5%를 언급했습니다.'
        payload = response(text, '수익률 4.6%를 언급했습니다.',
                           opinion='판단 불가', stage='unknown')
        reject(self, payload, source(text), 'event_reason_number_ungrounded')

    def test_invented_quote_or_recombined_sentences_are_rejected(self):
        text = '가상기업은 청약에 참여하겠습니다. 추가 의견입니다.'
        reject(self, response(text), source(text), 'event_evidence_not_choice')
        reject(self, response('추가 의견입니다.'), source(text), 'event_evidence_not_choice')

    def test_evidence_without_target_stock_or_alias_is_rejected(self):
        text = '공모주 검토 의견입니다.'
        payload = response(text, '검토 의견만 있습니다.', opinion='판단 불가', stage='unknown')
        reject(self, payload, source(text), 'event_evidence_not_target_stock')

    def test_registered_alias_counts_as_target_stock(self):
        text = 'DEMO 청약에 참여하겠습니다.'
        payload = response(text)
        self.assertIs(validate_event_response(payload, source(text, aliases=['가상기업', 'DEMO'])), payload)

    def test_fabricated_amount_is_rejected_even_with_other_numbers_present(self):
        text = ('가상기업 공모가 40,000원, 청약일은 2026-11-20입니다.\n'
                '가상기업은 청약에 참여하겠습니다.')
        payload = response('가상기업은 청약에 참여하겠습니다.',
                           '공모가 4만원이며 11월 20일 청약에 참여할 예정입니다.')
        reject(self, payload, source(text), 'event_reason_number_ungrounded')

    def test_fabricated_date_is_rejected(self):
        text = ('가상기업 공모가 40,000원, 청약일은 2026-11-20입니다.\n'
                '가상기업은 청약에 참여하겠습니다.')
        payload = response('가상기업은 청약에 참여하겠습니다.', '11월 21일 마감입니다.',
                           opinion='판단 불가', stage='unknown')
        reject(self, payload, source(text), 'event_reason_date_ungrounded')

    def test_coincidental_standalone_numbers_do_not_prove_a_full_date(self):
        text = '가상기업은 2026년 매출 11억원, 20명 규모입니다.\n가상기업 공모주 검토'
        payload = response('가상기업은 2026년 매출 11억원, 20명 규모입니다.',
                           '2026년 11월 20일 마감입니다.', opinion='판단 불가', stage='unknown')
        reject(self, payload, source(text), 'event_reason_date_ungrounded')

    def test_rating_or_title_evidence_cannot_make_a_participation_opinion(self):
        text = '가상기업 ⭐⭐'
        reject(self, response(text, '청약에 참여한다고 밝혔습니다.'), source(text),
               'event_opinion_ungrounded')

    def test_applied_stage_needs_explicit_execution_evidence(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        reject(self, response(text, '이미 신청했다고 밝혔습니다.', stage='applied'),
               source(text), 'event_stage_ungrounded')
        applied = '가상기업은 청약을 신청했습니다.'
        payload = response(applied, '청약 신청을 마쳤다고 밝혔습니다.', stage='applied')
        self.assertIs(validate_event_response(payload, source(applied)), payload)

    def test_non_participation_and_conditional_opinions_are_grounded(self):
        text = '가상기업은 청약하지 않겠습니다.'
        payload = response(text, '청약하지 않겠다고 밝혔습니다.', opinion='미참여', stage='planned')
        self.assertIs(validate_event_response(payload, source(text)), payload)
        conditional = '가상기업은 경쟁률이 낮으면 참여하겠습니다.'
        payload = response(conditional, '경쟁률에 따라 참여하겠다고 밝혔습니다.', opinion='조건부')
        self.assertIs(validate_event_response(payload, source(conditional)), payload)

    def test_reason_is_one_nonempty_sentence_within_180_characters(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        origin = source(text)
        reject(self, response(text, ''), origin, 'event_reason_empty')
        reject(self, response(text, '가' * 181), origin, 'event_reason_too_long')
        reject(self, response(text, '첫째입니다. 둘째입니다.'), origin, 'event_reason_multiple_sentences')

    def test_control_sequences_are_not_read_as_numeric_claims(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        payload = response(text, '\x1b[31m청약에 참여한다고 밝혔습니다.')
        self.assertIs(validate_event_response(payload, source(text)), payload)

    def test_generic_assessment_validation_remains_the_base_contract(self):
        text = '가상기업은 청약에 참여하겠습니다.'
        payload = response(text, '가' * 300)
        self.assertIs(validate_response(payload, source(text)), payload)
        with self.assertRaises(CollectionError):
            validate_event_response(payload, source(text))


class EvidenceChoicesTests(unittest.TestCase):
    def test_alias_sentences_are_truncated_deduplicated_and_capped(self):
        long_line = '가상기업 ' + '가' * 400
        text = '\n'.join([long_line, '가상기업 공모주 검토', '가상기업 공모주 검토'])
        self.assertEqual(evidence_choices(source(text)),
                         [long_line[:250], '가상기업 공모주 검토'])
        many = '\n'.join(f'가상기업 글 {number}' for number in range(25))
        self.assertEqual(len(evidence_choices(source(many))), 20)

    def test_first_line_is_used_when_no_sentence_names_the_stock(self):
        text = '공모주 검토\n다른 의견'
        self.assertEqual(evidence_choices(source(text)), ['공모주 검토'])
