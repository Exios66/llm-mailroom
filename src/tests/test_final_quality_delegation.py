"""Execute exact pilot facades and real Dojo field engines, with call evidence."""
import sys
import pytest


def _calls(function, *args, **kwargs):
    seen = set()
    def trace(frame, event, arg):
        if event == 'call':
            seen.add((frame.f_globals.get('__name__'), frame.f_code.co_name))
    old = sys.getprofile()
    try:
        sys.setprofile(trace)
        result = function(*args, **kwargs)
    finally:
        sys.setprofile(old)
    return result, seen


def test_subclass_and_normalizer_delegation():
    from scripts.run_hf_pilot import subclass_ok
    from langchain_agents.sorter_agent import normalize_subtype, equivalent_subtypes
    from langchain_agents.doc_inventories import normalize_record_type, normalize_sorter_subclass
    from langchain_agents.cuad_maud import normalize_consideration
    assert _calls(normalize_subtype, 'Joint Venture')[0] == 'joint_venture'
    cases = [
        ('contract','license',{'predicted_subtype':'maintenance'},True),
        ('contract','alien',{'predicted_subtype':'unknown'},True),
        ('contract','license',{},False),
        ('corporate_record','',{'predicted_subtype':'bylaws'},None),
        ('corporate_record','bylaws',{},False),
        ('corporate_record','alien',{'predicted_subtype':'unrecognized'},True),
        ('insurance_claim','auto',{'predicted_subtype':'','extracted':{'claim_type':'auto'}},True),
        ('correspondence','email',{'extracted':{'communication_type':'EMAIL'}},True),
        ('merger_agreement','all_cash',{'extracted':{'document_name':'per share in cash'}},True),
        ('merger_agreement','all_stock',{'predicted_subtype':'all_cash','extracted':{'merger_consideration':'all_stock'}},False),
    ]
    for kind,expected,kwargs,want in cases:
        got, seen = _calls(subclass_ok,kind,expected,**kwargs)
        assert got is want
        if expected:
            assert ('llm_dojo_scoring.pilot_labels','pilot_subclass_match') in seen
    for fn,args,name in [
        (normalize_subtype,('Joint Venture',),'normalize_pipeline_subtype'),
        (normalize_record_type,('Certificate of Incorporation',),'normalize_inventory_label'),
        (normalize_sorter_subclass,('corporate_record','bylaws'),'normalize_sorter_token'),
        (normalize_consideration,('cash',),'normalize_pilot_consideration'),
        (equivalent_subtypes,('license','maintenance'),'equivalent_subtypes'),
    ]:
        _, seen = _calls(fn,*args)
        assert any(m.startswith('llm_dojo_scoring.') and n == name for m,n in seen)


def test_summary_exact_population_and_real_delegation():
    from scripts.run_hf_pilot import summarize_rows
    rows = [
        {'expected':'contract','predicted':'contract','subclass_ok':True,'llm_tokens':0},
        {'expected':'contract','predicted':'merger_agreement','subclass_ok':False},
        {'expected':'corporate_record','predicted':'','exact_ok':True,'llm_cost_usd':0.0},
    ]
    out,seen = _calls(summarize_rows,rows)
    assert (out['n'],out['exact_n'],out['exact_accuracy'],out['subclass_n'],out['subclass_accuracy']) == (3,2,.667,2,.5)
    assert out['per_class']['contract']['exact_accuracy'] == .5
    assert out['per_class']['corporate_record']['subclass_accuracy'] is None
    assert out['total_tokens'] == 0
    assert ('llm_dojo_scoring.pilot_summary','pilot_classification_summary') in seen
    assert summarize_rows([])['subclass_accuracy'] is None


def test_pilot_presence_and_calibration_adapter():
    from scripts.run_pilot import _ground_truth_scores
    row = {'class_match':True,'stage_expected':False,'extracted_data':{'a':[],'b':False,'c':[None]},'classification_confidence':.3}
    out,seen = _calls(_ground_truth_scores,row,{'a':[],'b':False,'c':'x','blank':None})
    assert out == {'class_correct':1,'stage_correct':0,'expected_field_presence':.333,'confidence_calibration_error':.7}
    assert ('llm_dojo_scoring.field_support','pilot_ground_truth_scores') in seen


def test_schema_support_adapter_preserves_schema_order():
    from observability.specialist_suites import gt_schema_coverage,schema_fields
    names=list(schema_fields('correspondence'))
    expected={names[0]:'x',names[1]:False,names[2]:[],names[3]:None}
    out,seen=_calls(gt_schema_coverage,'correspondence',expected)
    assert out['n_schema'] == len(names)
    assert out['n_labeled'] == 2
    assert out['labeled_fields'] == names[:2]
    assert out['coverage'] == round(2/len(names),3)
    assert ('llm_dojo_scoring.field_support','schema_gt_support') in seen


def test_real_field_audit_emission_uses_canonical_support(monkeypatch):
    from observability import scores
    from observability.langfuse_field_scoring import score_and_log_extraction
    emitted=[]
    monkeypatch.setattr(scores,'is_enabled',lambda:True)
    monkeypatch.setattr(scores,'create_trace_score',lambda **kw:emitted.append(kw))
    result,seen = _calls(score_and_log_extraction,'trace','correspondence',
        {'sender':'name','subject_matter':'free_text'},
        {'sender':'Alice','subject_matter':'UNSUPPORTED'},
        {'sender':'Alice'},doc_text='Alice meeting')
    assert result.audit_support['n_audited'] == 2
    assert result.audit_support['hallucination_rate'] == .5
    values={v['name']:v for v in emitted}
    assert values['extraction_hallucination_rate']['value'] == .5
    assert values['extraction_overall_verified_precision']['value'] == .5
    assert 'n_audited=2' in values['extraction_hallucination_rate']['comment']
    assert ('llm_dojo_scoring.field_support','field_audit_support') in seen
    assert ('llm_dojo_scoring.field_scoring','score_extraction') in seen


def test_producer_equivalence_groups_do_not_follow_global_task_overrides(monkeypatch):
    from llm_dojo_scoring.config import get_settings
    from langchain_agents.sorter_agent import equivalent_subtypes
    from scripts.run_hf_pilot import subclass_ok
    monkeypatch.setattr(get_settings(),'subtype_equivalences',[])
    assert equivalent_subtypes('license','maintenance') is True
    assert subclass_ok('contract','license',predicted_subtype='maintenance') is True
