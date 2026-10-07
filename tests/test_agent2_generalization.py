"""Cross-domain safety regressions; only local fake providers, never Qwen."""
import asyncio
import json

import pytest

from agent2_asr import apply_corrections, resolve_corrections
from test_agent2_asr import FakeProvider, edit, runner


def correction_context(first, later, replacement, kind="number", alignment=None):
    units = [{"unit_id":"U0001", "text":first}, {"unit_id":"U0002", "text":later}]
    quote = f"For the same worked example use {replacement} as the parameter."
    item = edit(original=first.split('is ',1)[1].rstrip('.'),replacement=replacement,kind=kind,
        evidence=[{"page":1,"quote":quote}],
        alignment=alignment or {"page":1,"transcript_quote":first,"slide_quote":quote})
    value={"block_id":"B001","edits":[item],"additional_pages":[]}
    return value,dict(block_id="B001",units=units,evidence={"pages":[{"page":1,"text":quote}]},page_ids={1},expanded=False)


@pytest.mark.parametrize('label,old,new',[
    ('tax payment','$200','$2,000'),('interest rate','4.16%','4.06%'),
    ('timeout','10 ms','100 ms'),('concentration','2 mg','20 mg'),
    ('protocol version','rdt2.1','rdt2.2'),('library version','SDK1.2','SDK2.1'),
])
def test_another_value_in_same_block_does_not_prove_the_intended_parameter(label,old,new):
    value,context=correction_context(f'The {label} is {old}.',f'The next example uses {new}.',new)
    issues=resolve_corrections(value,**context)
    assert issues and value['edits'][0]['decision']=='flag'
    assert apply_corrections(context['units'],value['edits'])==context['units']


@pytest.mark.parametrize('old,new', [('4.16%','4.06%'),('$200','$2,000'),('10 ms','100 ms')])
def test_explicit_local_final_value_can_be_preserved(old,new):
    value,context=correction_context(f'The parameter is {old}.',f'Sorry, I mean {new} for this parameter.',new)
    assert resolve_corrections(value,**context)==[]
    assert value['edits'][0]['decision']=='apply'
    assert apply_corrections(context['units'],value['edits'])[0]['text']==f'The parameter is {new}.'


def test_unrelated_alignment_cannot_authorize_a_numeric_edit():
    value,context=correction_context('The rate is 4.16%.','Sorry, I mean 4.06% for this rate.','4.06%')
    value['edits'][0]['alignment']['transcript_quote']=context['units'][1]['text']
    issues=resolve_corrections(value,**context)
    assert issues and 'edited source unit' in issues[0]['error_message']
    assert value['edits'][0]['decision']=='flag'


@pytest.mark.parametrize('original,replacement', [('not valid','valid'),('loss','no loss'),('without permission','with permission')])
@pytest.mark.parametrize('explicit', [False,True])
def test_negation_requires_local_spoken_correction(original,replacement,explicit):
    later=f'Sorry, I mean {replacement} for this case.' if explicit else f'The material describes {replacement} in another case.'
    value,context=correction_context(f'The condition is {original}.',later,replacement,kind='wording')
    resolve_corrections(value,**context)
    assert value['edits'][0]['decision']==('apply' if explicit else 'flag')
    if not explicit: assert apply_corrections(context['units'],value['edits'])==context['units']


@pytest.mark.parametrize('later',[
    'Sorry about the microphone. The next example uses 4.06%.',
    'Actually the next loan has a rate of 4.06%.',
    'This second loan uses 4.06%. Sorry about the microphone.',
])
def test_discourse_markers_do_not_authorize_values_from_a_new_sentence(later):
    value,context=correction_context('The rate is 4.16%.',later,'4.06%')
    resolve_corrections(value,**context)
    assert value['edits'][0]['decision']=='flag'


def test_summary_receives_flags_and_rejected_proposals_separately_from_speech(tmp_path):
    captured={}
    class Capture(FakeProvider):
        async def create_chat_completion(self,**kwargs):
            prompt=kwargs['messages'][0]['content']
            payload=json.loads(kwargs['messages'][1]['content'])
            if prompt.startswith('You correct'):
                raw=json.loads(await super().create_chat_completion(**kwargs))
                if raw['block_id']=='B001':
                    raw['edits'].append(edit(unit_id='U0002',original='discount',replacement='capitalize',evidence=[]))
                assert payload['evidence_provenance']['nodes']=='generated_scaffold_fallible_not_primary_evidence'
                return json.dumps(raw)
            if prompt.startswith('You summarize'):
                captured[payload['block_id']]=payload
            return await super().create_chat_completion(**kwargs)
    run=runner(tmp_path,Capture())
    assert asyncio.run(run.run())['status']=='completed'
    first=captured['B001']
    assert first['unresolved_corrections'][0]['replacement']=='capitalize'
    assert first['validation_issues'][0]['action']=='flagged'
    assert first['transcript_units'][1]['text']=='We discount future cash flows.'
    assert 'capitalize' not in first['transcript']
    assert captured['B002']['unresolved_corrections'][0]['original']=='4.16%'
