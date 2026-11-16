"""No-network, deterministic tests of the experimental priority policy."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from ipo_monitor.decision import DecisionError, evaluate, load_policy


def signal(source='blog', creator='팽', kind='rss', opinion='판단 불가', assessment='unknown',
           rating=None, marker='', snapshot=1, observed='2026-11-15T09:00:00+09:00',
           status='success', stage='planned', source_status='success'):
    return {'stock':'가상기업', 'source_id':source, 'source_type':kind, 'creator':creator,
            'snapshot_id':snapshot, 'observed_at':observed, 'url':'https://example.invalid/1',
            'opinion':opinion, 'assessment':assessment, 'evidence':'가상기업 의견',
            'stage':stage, 'rating':rating, 'marker':marker,
            'analysis_status':status, 'source_status':source_status,
            'error':None if status=='success' else 'fixture_invalid'}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.policy=load_policy('config/decision.toml')

    def test_unknown_is_not_zero_rating_or_negative(self):
        unknown=evaluate([signal()],self.policy)
        self.assertIsNone(unknown['score'])
        self.assertEqual(unknown['status'],'판단 대기')
        self.assertEqual(unknown['coverage'],0)
        zero=evaluate([signal(rating=0)],self.policy)
        cross=evaluate([signal(marker='❌')],self.policy)
        self.assertEqual(zero['score'],-1)
        self.assertEqual(cross['score'],-1)
        self.assertEqual(zero['status'],'낮은 우선순위')

    def test_fields_are_averaged_not_independent_votes(self):
        value=evaluate([signal(opinion='참여',assessment='positive',rating=2)],self.policy)
        self.assertAlmostEqual(value['score'],2/3,places=9)
        self.assertEqual(value['coverage'],0.5)
        self.assertEqual(value['status'],'검토 후보')

    def test_condition_and_neutral_preserved(self):
        value=evaluate([signal(opinion='조건부')],self.policy)
        self.assertEqual(value['score'],0.25)
        self.assertEqual(value['status'],'관찰')
        self.assertEqual(value['contributions'][0]['opinion'],'조건부')
        self.assertEqual(evaluate([signal(assessment='neutral')],self.policy)['score'],0)

    def test_same_creator_multiple_channels_not_double_counted(self):
        one=[signal(opinion='참여'), signal('youtube','기릿','youtube',assessment='neutral')]
        two=one+[signal('site','기릿','ipo_site',assessment='neutral')]
        self.assertEqual(evaluate(one,self.policy)['score'],0.5)
        self.assertEqual(evaluate(two,self.policy)['score'],0.5)
        self.assertEqual(evaluate(two,self.policy)['coverage'],1)

    def test_source_weights_apply_inside_creator(self):
        rows=[signal('youtube','기릿','youtube',opinion='참여'),
              signal('site','기릿','ipo_site',assessment='neutral')]
        self.policy['source_weights']['youtube']=3
        self.assertEqual(evaluate(rows,self.policy)['score'],0.75)

    def test_creator_weights_apply_after_grouping(self):
        rows=[signal(opinion='참여'), signal('youtube','기릿','youtube',assessment='neutral')]
        self.policy['creator_weights']['팽']=3
        self.assertEqual(evaluate(rows,self.policy)['score'],0.75)

    def test_conflict_keeps_score_and_requires_check(self):
        result=evaluate([signal(opinion='참여'),
            signal('youtube','기릿','youtube',opinion='미참여')],self.policy)
        self.assertEqual(result['score'],0)
        self.assertTrue(result['conflict'])
        self.assertEqual(result['status'],'추가 확인')
        self.assertEqual(len(result['contributions']),2)

    def test_latest_per_source_only_and_superseded_evidence_retained(self):
        old=signal(opinion='미참여')
        new=signal(opinion='참여',snapshot=2)
        result=evaluate([old,new],self.policy)
        self.assertEqual(result['score'],1)
        self.assertFalse(result['conflict'])
        self.assertEqual([c['reason'] for c in result['contributions']],['superseded','scored'])

    def test_actual_time_offset_not_lexical_date_selects_latest(self):
        older=signal(opinion='미참여',snapshot=2,observed='2026-11-15T10:00:00+09:00')
        newer=signal(opinion='참여',snapshot=1,observed='2026-11-15T02:00:00+00:00')
        self.assertEqual(evaluate([older,newer],self.policy)['score'],1)

    def test_latest_failure_does_not_fall_back_to_success(self):
        rows=[signal(opinion='참여'), signal(snapshot=2,status='error')]
        result=evaluate(rows,self.policy)
        self.assertIsNone(result['score'])
        self.assertEqual(result['status'],'판단 대기')
        self.assertEqual(result['contributions'][-1]['reason'],'error')

    def test_unprocessed_and_error_do_not_create_negative_scores(self):
        rows=[signal(status='unprocessed'),signal('site','기릿','ipo_site',rating=0,status='error')]
        result=evaluate(rows,self.policy)
        self.assertIsNone(result['score'])
        self.assertEqual(result['coverage'],0)
        self.assertEqual(result['status'],'판단 대기')

    def test_retrospective_factor_is_explicit_and_adjustable(self):
        result=evaluate([signal(opinion='참여',stage='retrospective')],self.policy)
        self.assertEqual(result['score'],0.25)
        self.assertEqual(result['contributions'][0]['stage_factor'],0.25)
        self.policy['stage_factors']['retrospective']=1
        self.assertEqual(evaluate([signal(opinion='참여',stage='retrospective')],self.policy)['score'],1)

    def test_zero_weight_is_preserved_but_no_vote(self):
        self.policy['source_weights']['rss']=0
        result=evaluate([signal(opinion='참여')],self.policy)
        self.assertIsNone(result['score'])
        self.assertEqual(result['contributions'][0]['reason'],'zero_weight')

    def test_minimum_coverage_and_thresholds_configurable(self):
        self.policy['thresholds']['minimum_coverage']=0.75
        self.assertEqual(evaluate([signal(opinion='참여')],self.policy)['status'],'추가 확인')
        self.policy['thresholds']['minimum_coverage']=0.5
        self.policy['thresholds']['candidate']=0.8
        self.assertEqual(evaluate([signal(assessment='positive')],self.policy)['status'],'관찰')

    def test_unknown_creator_adds_coverage_slot_not_hidden_vote(self):
        rows=[signal(opinion='참여'),signal('third','다른작성자','youtube',opinion='참여')]
        result=evaluate(rows,self.policy)
        self.assertAlmostEqual(result['coverage'],2/3,places=9)
        self.assertEqual(result['score'],1)

    def test_empty_result_and_order_independence(self):
        result=evaluate([],self.policy)
        self.assertEqual(result['status'],'판단 대기')
        self.assertEqual(result['contributions'],[])
        rows=[signal(opinion='참여'),signal('youtube','기릿','youtube',rating=2)]
        self.assertEqual(evaluate(rows,self.policy),evaluate(list(reversed(rows)),self.policy))

    def test_invalid_policy_types_ranges_and_unknown_keys(self):
        bad=[]
        for value in (True,float('nan'),float('inf'),-1,10**1000):
            policy=deepcopy(self.policy);policy['source_weights']['rss']=value;bad.append(policy)
        policy=deepcopy(self.policy);policy['thresholds']['candidate']=-0.8;bad.append(policy)
        policy=deepcopy(self.policy);policy['opinion_scores']['판단 불가']=-1;bad.append(policy)
        policy=deepcopy(self.policy);policy['expected_creators']=['팽','팽'];bad.append(policy)
        policy=deepcopy(self.policy);policy['stage_factors']['planned']=2;bad.append(policy)
        for policy in bad:
            with self.subTest(policy=policy),self.assertRaises(DecisionError):evaluate([],policy)

    def test_invalid_file_does_not_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'policy.toml'
            with self.assertRaises(DecisionError):load_policy(path)
            path.write_text('version = [')
            with self.assertRaises(DecisionError):load_policy(path)
            path.write_text('version = "missing fields"')
            with self.assertRaises(DecisionError):load_policy(path)

    def test_invalid_signal_and_mixed_stock_rejected(self):
        for key,value in [('snapshot_id',True),('rating',True),('rating',4),
                          ('observed_at','2026-11-15T09:00:00'),('opinion','패스')]:
            row=signal();row[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(DecisionError):evaluate([row],self.policy)
        other=signal('other');other['stock']='다른기업'
        with self.assertRaisesRegex(DecisionError,'stock_mismatch'):evaluate([signal(),other],self.policy)

    def test_conflicting_duplicate_or_changed_source_identity_rejected(self):
        with self.assertRaisesRegex(DecisionError,'duplicate_signal'):
            evaluate([signal(opinion='참여'),signal(opinion='미참여')],self.policy)
        with self.assertRaisesRegex(DecisionError,'identity_mismatch'):
            evaluate([signal(),signal(creator='기릿',snapshot=2)],self.policy)

    def test_source_issue_keeps_score_but_holds_status(self):
        row=signal(opinion='참여',source_status='error')
        row.update(analysis_id=17,input_hash='test',reason='source check failed')
        result=evaluate([row],self.policy)
        self.assertEqual(result['score'],1)
        self.assertEqual(result['status'],'추가 확인')
        self.assertEqual(result['source_issues'][0]['status'],'error')
        self.assertEqual(result['contributions'][0]['analysis_id'],17)
        self.policy['thresholds']['hold_on_source_issue']=False
        self.assertEqual(evaluate([row],self.policy)['status'],'검토 후보')

    def test_source_issue_without_numeric_evidence_still_visible(self):
        result=evaluate([signal(source_status='partial')],self.policy)
        self.assertIsNone(result['score'])
        self.assertEqual(result['status'],'추가 확인')

    def test_superseded_source_issue_does_not_hold_current_success(self):
        result=evaluate([signal(source_status='error'),signal(opinion='참여',snapshot=2)],self.policy)
        self.assertEqual(result['source_issues'],[])
        self.assertEqual(result['status'],'검토 후보')

    def test_hold_flag_is_strict_boolean_and_signal_enums_safe(self):
        self.policy['thresholds']['hold_on_source_issue']=1
        with self.assertRaises(DecisionError):evaluate([],self.policy)
        self.policy['thresholds']['hold_on_source_issue']=True
        for field in ('opinion','assessment','stage','analysis_status','source_status'):
            row=signal();row[field]=[]
            with self.subTest(field=field),self.assertRaises(DecisionError):evaluate([row],self.policy)
