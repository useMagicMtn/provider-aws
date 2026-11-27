#!/usr/bin/env python3
# Subprocess fixture: validates Rust's bridge payload, never accesses AWS.
import json
import sys
request, credential = json.load(sys.stdin)
op = request['operation']
if credential is not None:
    assert credential == {'AWS_ACCESS_KEY_ID': 'test-key', 'AWS_SECRET_ACCESS_KEY': 'test-secret'}
if op == 'verify_account':
    assert credential == {'AWS_ACCESS_KEY_ID': 'test-key', 'AWS_SECRET_ACCESS_KEY': 'test-secret'}
    value = '123456789012'
elif op == 'configure':
    assert credential is not None
    value = {'account':'123456789012','configuration':{'MAGICMTN_AWS_ACCOUNT_ID':'123456789012','MAGICMTN_AWS_REGION':'us-east-2','MAGICMTN_AWS_BEDROCK_REGION':'us-east-2'}}
elif op == 'prepare_image':
    image = request['images']['linux/arm64']
    value = {'image':image,'platform':'linux/arm64','artifact':'arn:aws:lambda:us-east-2:123456789012:microvm-image:image-test@1.0','configuration':{'MAGICMTN_AWS_MICROVM_IMAGES':json.dumps({image['repository']+'@'+image['digest']:'image-test@1.0'})}}
elif op == 'sandbox_status':
    value = 'running'
elif op == 'sandbox_create':
    value = {'provider_id':'mvm-test','status':'PENDING','create_time_ms':None,'metadata':{'region':'us-east-1'}}
elif op == 'exec':
    assert request['command'] == 'echo' and request['args'] == ['literal;雪']
    value = {'stdout':'literal;雪\n','stderr':'','exit_code':0,'elapsed_ms':1,'timed_out':False,'truncated':False}
elif op == 'infer' and request['target'].startswith('us.openai.'):
    assert request['body']['model'] == request['target']
    assert request['body']['max_completion_tokens'] == 100
    assert 'max_tokens' not in request['body']
    assert request['body']['stream'] is False
    value = {'model':request['target'],'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'function':{'name':'run','arguments':{'x':1}}}]},'finish_reason':'tool_calls'}], 'usage':{'prompt_tokens':10,'completion_tokens':5}}
elif op == 'infer':
    assert request['body']['stream'] is False
    assert request['body']['model'] == 'org/model'
    assert isinstance(request['body']['messages'][0]['tool_calls'][0]['function']['arguments'], str)
    value = {'model':'org/model','choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'function':{'name':'run','arguments':{'x':1}}}]},'finish_reason':'tool_calls'}]}
elif op == 'deployment_create' and request['spec']['model'].startswith('us.openai.'):
    model = request['spec']['model']
    value = {'resource':{'provider_id':'bedrock/123456789012/us-east-2/profiles/'+model},'model':model,'inference_model':model,'state':'ready','ready_replicas':None}
elif op == 'deployment_create':
    value = {'resource':{'provider_id':'sagemaker/test-model/models/org/model'},'model':'org/model','inference_model':'sagemaker/test-model/models/org/model','state':'stopped','ready_replicas':0}
elif op in ('deployment_start', 'deployment_status', 'deployment_stop'):
    state = {'deployment_start':'starting','deployment_status':'ready','deployment_stop':'stopping'}[op]
    value = {'resource':request['resource'],'model':'org/model','inference_model':request['resource']['provider_id'],'state':state,'ready_replicas':None}
else:
    value = None
json.dump(value, sys.stdout)
