"""Execute the compatibility verdict facade against the real Dojo policy."""
import sys
import pytest
from observability.scores import deterministic_verdict_label
from pipeline import config


def _call(*args,**kwargs):
    """Observe real canonical function entries without replacing the scorer."""
    seen=set()
    def observe(frame,event,arg):
        """Collect function-entry identities during this call."""
        if event=='call': seen.add((frame.f_globals.get('__name__'),frame.f_code.co_name))
    old=sys.getprofile()
    try:
        sys.setprofile(observe)
        result=deterministic_verdict_label(*args,**kwargs)
    finally:
        sys.setprofile(old)
    assert ('llm_dojo_scoring.pipeline_verdict','deterministic_verdict_label') in seen
    return result


@pytest.mark.parametrize('band,score,want', [([.2,.8],.8,'CORRECT'),([.2,.8],.2,'MISS'),
    ([.2,.8],.5,'PARTIAL'),(['bad',.1],.85,'CORRECT'),([.9,.4],.7,'CORRECT'),
    ([],.5,'MISS'),(None,None,'PARTIAL')])
def test_real_facade_delegates_configured_thresholds(monkeypatch,band,score,want):
    """Only configuration I/O is doubled; real verdict logic executes."""
    def load():
        """Return the operator's configured band."""
        return {'field_scoring':{'ambiguous_band':band}}
    monkeypatch.setattr(config,'load_config',load)
    assert _call(score)==want


def test_config_failure_and_early_priority(monkeypatch):
    """Broken config falls back, while mismatch/review/None avoid reading it."""
    calls=[]
    def broken():
        """Fail a configuration read while recording the attempt."""
        calls.append(1)
        raise RuntimeError('unreadable config')
    monkeypatch.setattr(config,'load_config',broken)
    assert _call(None)=='PARTIAL'
    assert _call('bad',needs_judge_review=True)=='PARTIAL'
    assert _call(None,needs_judge_review=True,class_mismatch=True)=='MISS'
    assert calls==[]
    assert _call(.85)=='CORRECT' and calls==[1]
    with pytest.raises(ValueError,match='could not convert string to float'):
        _call('bad')
