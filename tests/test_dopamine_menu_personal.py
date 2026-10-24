"""Private, synthetic menu controls; no voice, model, GPIO or live store."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from pxh import people, provenance

ROOT = Path(__file__).parents[1]
TOOL = ROOT / 'bin/tool-dopamine-menu'


def load_tool():
    source = TOOL.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    scope = {'__name__': 'offline_menu_tool'}
    exec(compile(source, str(TOOL), 'exec'), scope)
    return scope


@pytest.fixture
def menu_env(tmp_path, monkeypatch):
    state, logs = tmp_path/'state', tmp_path/'logs'
    state.mkdir()
    session = state/'session.json'
    session.write_text('{"persona":"spark"}')
    for key,value in {'PROJECT_ROOT':ROOT,'PX_STATE_DIR':state,'PX_SESSION_PATH':session,
                      'LOG_DIR':logs,'PX_ENV_SKIP_DOTENV':'1','PX_DRY':'0'}.items():
        monkeypatch.setenv(key,str(value))
    return state,logs,session


def request(item='magnetic tiles', action='add', **overrides):
    text = f'{"Add" if action=="add" else "Remove"} {item} {"to" if action=="add" else "from"} my dopamine menu'
    return dict(role='user',text=text,persona='spark',menu={'action':action,'item':item,
                'energy':'medium','context':'free'},**overrides)


def test_structured_store_provenance_removal_and_readdition(menu_env):
    assert people.record_person_facts(**request()) == 1
    assert people.dopamine_items('medium','free') == ['magnetic tiles']
    first=people.read_people('spark')[0]
    assert first['subject']=='obi' and first['fact_kind']=='preference'
    assert provenance.read_provenance(first)['kind']=='report'
    assert provenance.read_provenance(first)['confidence'] <= .9
    assert people.record_person_facts(**request(action='remove')) == 1
    assert people.dopamine_items('medium','free') == []
    assert len(people.load_people())==2
    assert provenance.is_superseded(people.read_people()[0])
    assert 'requested menu removal' in people.render_person_fact(people.read_people()[-1])
    assert people.record_person_facts(**request())==1
    assert people.dopamine_items('medium','free')==['magnetic tiles']
    assert not list(menu_env[0].glob('notes*'))


@pytest.mark.parametrize('text', ["My friend said add magnetic tiles to my dopamine menu",
    'Can you add magnetic tiles to my dopamine menu?',
    'Add dinosaurs to my dopamine menu', 'Add magnetic tiles to my dopamine menu. I am sad',
    '"Add magnetic tiles to my dopamine menu"'])
def test_model_proposal_cannot_manufacture_a_human_request(menu_env,text):
    data=request();data['text']=text
    assert people.record_person_facts(**data)==0
    assert not people.people_file().exists()


def test_categories_subject_and_persona_are_isolated(menu_env):
    data=request();data['text']='Please add magnetic tiles to my high energy focus dopamine menu.'
    data['menu'].update(energy='high',context='focus')
    assert people.record_person_facts(**data)==1
    assert people.dopamine_items('medium','free')==[]
    assert people.dopamine_items('high','focus')==['magnetic tiles']
    assert people.dopamine_items('high','focus',subject='someone-else')==[]
    assert people.dopamine_items('high','focus',persona='vixen')==[]
    data['persona']='gremlin'
    assert people.record_person_facts(**data)==0


def test_concurrent_requests_form_one_supersession_chain(menu_env):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _:people.record_person_facts(**request()),range(2)))==[1,1]
    live=[r for r in people.read_people() if not provenance.is_superseded(r)]
    assert len(live)==1 and len(people.load_people())==2


def test_tool_add_remove_and_suggest_use_actual_store(menu_env,monkeypatch,capsys):
    scope=load_tool();calls=[]
    scope['speak']=lambda text,dry:calls.append(('voice',text))
    scope['emote']=lambda name,dry:calls.append(('emote',name))
    for key,value in {'PX_DOPAMINE_ACTION':'add','PX_DOPAMINE_ITEM':'magnetic tiles',
                      'PX_DOPAMINE_REQUEST':request()['text']}.items():monkeypatch.setenv(key,value)
    assert scope['main']()==0
    assert json.loads(capsys.readouterr().out)['applied'] is True
    monkeypatch.setenv('PX_DOPAMINE_ACTION','suggest')
    assert scope['main']()==0
    assert 'magnetic tiles' in json.loads(capsys.readouterr().out)['picks']
    monkeypatch.setenv('PX_DOPAMINE_ACTION','remove')
    monkeypatch.setenv('PX_DOPAMINE_REQUEST',request(action='remove')['text'])
    assert scope['main']()==0
    assert people.dopamine_items('medium','free')==[]
    assert calls


def test_tool_refusal_never_confirms_or_calls_children(menu_env,monkeypatch,capsys):
    scope=load_tool()
    scope['speak']=scope['emote']=lambda *a:pytest.fail('unexpected child action')
    monkeypatch.setenv('PX_DOPAMINE_ACTION','add');monkeypatch.setenv('PX_DOPAMINE_ITEM','imagined activity')
    monkeypatch.delenv('PX_DOPAMINE_REQUEST',raising=False)
    assert scope['main']()==1
    assert json.loads(capsys.readouterr().out)['status']=='error'
    assert not people.people_file().exists()


@pytest.mark.parametrize('action',['add','remove','suggest'])
def test_actual_cli_dry_creates_no_state_logs_or_session(tmp_path,action):
    state,logs=tmp_path/'missing-state',tmp_path/'missing-logs'
    env=dict(os.environ,PX_DRY='1',PX_STATE_DIR=str(state),PX_SESSION_PATH=str(state/'session.json'),
             LOG_DIR=str(logs),PX_DOPAMINE_ACTION=action,PX_DOPAMINE_ITEM='magnetic tiles',
             PX_ENV_SKIP_DOTENV='1',PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([str(TOOL)],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout.splitlines()[-1]);assert payload['dry'] is True
    assert not state.exists() and not logs.exists()
    if action!='suggest':assert payload['applied'] is False


def test_dry_existing_data_and_session_bytes_stay_unchanged(menu_env,monkeypatch):
    people.record_person_facts(**request())
    before={p.relative_to(menu_env[0]):p.read_bytes() for p in menu_env[0].rglob('*') if p.is_file()}
    scope=load_tool();scope['speak']=scope['emote']=lambda *a:pytest.fail('dry invoked a child')
    monkeypatch.setenv('PX_DRY','1')
    assert scope['main']()==0
    after={p.relative_to(menu_env[0]):p.read_bytes() for p in menu_env[0].rglob('*') if p.is_file()}
    assert after==before


def test_removed_builtin_is_not_offered_and_restore_is_explicit(menu_env,monkeypatch,capsys):
    data=request(item='Reading',action='remove');data['text']='Remove Reading from my medium wind-down dopamine menu';data['menu']['context']='wind-down'
    assert people.record_person_facts(**data)==1
    scope=load_tool();monkeypatch.setenv('PX_DRY','1');monkeypatch.setenv('PX_DOPAMINE_CONTEXT','wind-down')
    assert scope['main']()==0
    assert 'Reading.' not in json.loads(capsys.readouterr().out)['picks']
    data['text']='Add Reading to my medium wind-down dopamine menu';data['menu']['action']='add'
    assert people.record_person_facts(**data)==1
    assert people.dopamine_items('medium','wind-down')==['Reading']
    assert people.dopamine_items('medium','wind-down',removed=True)==[]


@pytest.mark.parametrize('persona',['gremlin','vixen','../../spark'])
def test_tool_never_reads_private_store_for_other_personas(menu_env,monkeypatch,capsys,persona):
    menu_env[2].write_text(json.dumps({'persona':persona}))
    scope=load_tool()
    monkeypatch.setattr(people,'dopamine_items',lambda *a,**k:pytest.fail('private store was read'))
    scope['speak']=scope['emote']=lambda *a:pytest.fail('child invoked')
    assert scope['main']()==1
    assert json.loads(capsys.readouterr().out)['status']=='error'


def test_large_or_nul_request_is_not_stored(menu_env):
    for text in ['Add magnetic tiles'+' '*600+'to my dopamine menu',
                 'Add magnetic'+chr(0)+' tiles to my dopamine menu']:
        data=request();data['text']=text
        assert people.record_person_facts(**data)==0
    assert not people.people_file().exists()


def test_validator_keeps_current_audio_classification_and_rejects_untrusted_source(menu_env,monkeypatch):
    from pxh import voice_loop
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(voice_loop,'_policy_now',lambda:datetime(2026,10,24,12,tzinfo=ZoneInfo('Australia/Hobart')).timestamp())
    monkeypatch.setattr(voice_loop,'_load_awareness_for_policy',lambda:{})
    monkeypatch.setattr(voice_loop.policy_context,'wake_grant_active',lambda:True)
    monkeypatch.setattr(voice_loop.policy_context,'wake_grant_confirmed',lambda:True)
    tool,env=voice_loop.validate_action({'tool':'tool_dopamine_menu','params':{
        'action':'add','item':'magnetic tiles','request':'model-invented source'}})
    assert tool=='tool_dopamine_menu'
    assert env['PX_DOPAMINE_ACTION']=='add' and env['PX_DOPAMINE_ITEM']=='magnetic tiles'
    assert 'PX_DOPAMINE_REQUEST' not in env
    assert voice_loop.classify_effect(tool,{'action':'add'})=='audio'
    with pytest.raises(voice_loop.VoiceLoopError):
        voice_loop.validate_action({'tool':tool,'params':{'action':'add','item':'bad'+chr(0)}})


@pytest.mark.parametrize('broken',['missing','invalid','nonobject','recovered'])
def test_unavailable_persona_never_grants_private_access(menu_env,monkeypatch,capsys,broken):
    from pxh.state import RECOVERED_KEY
    if broken=='missing':menu_env[2].unlink()
    elif broken=='invalid':menu_env[2].write_text('broken JSON')
    elif broken=='nonobject':menu_env[2].write_text('[]')
    else:menu_env[2].write_text(json.dumps({'persona':'spark',RECOVERED_KEY:True}))
    before=menu_env[2].read_bytes() if menu_env[2].exists() else None
    scope=load_tool()
    monkeypatch.setattr(people,'dopamine_items',lambda *a,**k:pytest.fail('private store read with unknown scope'))
    scope['speak']=scope['emote']=lambda *a:pytest.fail('unknown scope invoked a child')
    assert scope['main']()==1
    capsys.readouterr()
    monkeypatch.setenv('PX_DRY','1')
    assert scope['main']()==0
    assert json.loads(capsys.readouterr().out)['dry'] is True
    assert (menu_env[2].read_bytes() if menu_env[2].exists() else None)==before


def test_dotenv_cannot_supply_forged_human_evidence(tmp_path):
    import shutil,sys
    project=tmp_path/'project';bindir=project/'bin';bindir.mkdir(parents=True)
    for name in ['tool-dopamine-menu','px-env']:shutil.copy2(ROOT/'bin'/name,bindir/name)
    (project/'src').symlink_to(ROOT/'src',target_is_directory=True)
    state=project/'state';state.mkdir();(state/'session.json').write_text('{"persona":"spark"}')
    (project/'.env').write_text('PX_DOPAMINE_REQUEST="Add magnetic tiles to my dopamine menu"\n')
    canary=project/'unexpected-child'
    for name in ['tool-voice','tool-emote']:
        path=bindir/name;path.write_text('#!/bin/sh\ntouch "'+str(canary)+'"\nexit 99\n');path.chmod(0o755)
    env=dict(os.environ,PX_DRY='0',PX_STATE_DIR=str(state),PX_SESSION_PATH=str(state/'session.json'),
             LOG_DIR=str(project/'logs'),PX_DOPAMINE_ACTION='add',PX_DOPAMINE_ITEM='magnetic tiles',
             PATH=str(Path(sys.executable).parent)+':'+os.environ['PATH'])
    env.pop('PX_DOPAMINE_REQUEST',None);env.pop('PX_ENV_SKIP_DOTENV',None)
    result=subprocess.run([str(bindir/'tool-dopamine-menu')],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==1,result.stdout+result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['status']=='error'
    assert not (state/'people-spark.jsonl').exists() and not canary.exists()


def test_executor_discards_ambient_request_but_keeps_trusted_binding(menu_env,monkeypatch):
    from pxh import voice_loop
    monkeypatch.setattr(voice_loop,'load_session',lambda:{'persona':'spark'})
    monkeypatch.setenv('PX_DOPAMINE_REQUEST','ambient invented request')
    calls=[]
    def capture(command,**kw):
        calls.append(kw['env']);return subprocess.CompletedProcess(command,0,'{}','')
    monkeypatch.setattr(voice_loop.subprocess,'run',capture)
    voice_loop.execute_tool('tool_dopamine_menu',{},True)
    assert 'PX_DOPAMINE_REQUEST' not in calls[-1]
    actual=request()['text']
    voice_loop.execute_tool('tool_dopamine_menu',{'PX_DOPAMINE_REQUEST':actual},True)
    assert calls[-1]['PX_DOPAMINE_REQUEST']==actual


def test_unreadable_existing_ledger_cannot_confirm_a_change(menu_env,monkeypatch):
    assert people.record_person_facts(**request(action='remove'))==1
    ledger=people.people_file();before=ledger.read_bytes();original=Path.read_text
    def unavailable(path,*a,**k):
        if path==ledger:raise PermissionError('synthetic ledger read failure')
        return original(path,*a,**k)
    monkeypatch.setattr(Path,'read_text',unavailable)
    assert people.record_person_facts(**request())==0
    assert ledger.read_bytes()==before


def test_write_failure_is_not_acknowledged(menu_env,monkeypatch,capsys):
    scope=load_tool();scope['speak']=scope['emote']=lambda *a:pytest.fail('failed write confirmed')
    monkeypatch.setenv('PX_DOPAMINE_ACTION','add');monkeypatch.setenv('PX_DOPAMINE_ITEM','magnetic tiles')
    monkeypatch.setenv('PX_DOPAMINE_REQUEST',request()['text'])
    monkeypatch.setattr(people,'append_person_facts',lambda *a,**k:(_ for _ in ()).throw(OSError('synthetic full store')))
    assert scope['main']()==1
    assert json.loads(capsys.readouterr().out)['status']=='error'


def test_child_helpers_receive_no_raw_human_request(menu_env,monkeypatch):
    scope=load_tool();monkeypatch.setenv('PX_DOPAMINE_REQUEST','private explicit human request')
    calls=[]
    monkeypatch.setattr(scope['subprocess'],'run',lambda *a,**k:calls.append(k['env']))
    scope['speak']('Added that to your menu.',False);scope['emote']('curious',False)
    assert len(calls)==2 and all('PX_DOPAMINE_REQUEST' not in env for env in calls)
