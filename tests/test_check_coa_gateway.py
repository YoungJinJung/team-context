from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import boto3
import pytest
from botocore.stub import Stubber

from scripts.check_coa_gateway import check
from scripts import check_coa_gateway

RUNTIME='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/CoaRuntime-0123456789'
GATEWAY='arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gateway-123'
RID='CoaRuntime-0123456789'
TIME=datetime(2026,1,1,tzinfo=timezone.utc)


def endpoint(name='DEFAULT', version='1'):
    return {'name':name,'agentRuntimeArn':RUNTIME,'agentRuntimeEndpointArn':RUNTIME+'/runtime-endpoint/'+name,
            'status':'READY','liveVersion':version,'targetVersion':version,'id':'Test-0123456789',
            'createdAt':TIME,'lastUpdatedAt':TIME}


def runtime(version='1'):
    return {'agentRuntimeArn':RUNTIME,'agentRuntimeId':RID,'agentRuntimeVersion':version,'status':'READY',
            'agentRuntimeName':'CoaRuntime','createdAt':TIME,'lastUpdatedAt':TIME,
            'roleArn':'arn:aws:iam::123456789012:role/runtime','lifecycleConfiguration':{},
            'authorizerConfiguration':{'customJWTAuthorizer':{'discoveryUrl':'https://issuer.example/.well-known/openid-configuration',
            'allowedWorkloadConfiguration':{'hostingEnvironments':[{'arn':GATEWAY}]}}}}


def client():
    value=Mock();value.meta.region_name='us-east-1'
    value.list_agent_runtime_endpoints.return_value={'runtimeEndpoints':[endpoint()]}
    value.get_agent_runtime.return_value=runtime()
    return value


def test_checks_all_endpoints_and_each_live_version():
    api=client();items=[endpoint(),endpoint('PROD','2')]
    api.list_agent_runtime_endpoints.return_value={'runtimeEndpoints':items}
    api.get_agent_runtime.side_effect=lambda agentRuntimeId,agentRuntimeVersion: runtime(agentRuntimeVersion)
    assert check(api,RUNTIME,GATEWAY) is True
    assert api.list_agent_runtime_endpoints.call_count==2
    assert {call.kwargs['agentRuntimeVersion'] for call in api.get_agent_runtime.call_args_list}=={'1','2'}


def test_uses_real_botocore_api_contract_without_aws_credentials():
    api=boto3.client('bedrock-agentcore-control',region_name='us-east-1',aws_access_key_id='fake',aws_secret_access_key='fake')
    with Stubber(api) as stub:
        page={'runtimeEndpoints':[endpoint()]}
        expected={'agentRuntimeId':RID,'maxResults':100}
        stub.add_response('list_agent_runtime_endpoints',page,expected)
        stub.add_response('get_agent_runtime',runtime(),{'agentRuntimeId':RID,'agentRuntimeVersion':'1'})
        stub.add_response('list_agent_runtime_endpoints',page,expected)
        assert check(api,RUNTIME,GATEWAY) is True
        stub.assert_no_pending_responses()


@pytest.mark.parametrize('resource,gateway',[(None,GATEWAY),(RUNTIME,'bad'),(RUNTIME,GATEWAY.replace('123456789012','999999999999')),(RUNTIME,GATEWAY.replace('us-east-1','us-west-2')),(RUNTIME,GATEWAY+'*')])
def test_rejects_untrusted_arns_without_aws_calls(resource,gateway):
    api=client()
    with pytest.raises(ValueError,match='^Unable to verify gateway-only runtime configuration$'): check(api,resource,gateway)
    api.list_agent_runtime_endpoints.assert_not_called()


@pytest.mark.parametrize('change',[
    {'status':'UPDATING'}, {'liveVersion':'0'}, {'targetVersion':'2'},
    {'agentRuntimeArn':'other'}, {'agentRuntimeEndpointArn':'other'}, {'name':'PROD/../DEFAULT'},
])
def test_rejects_unstable_or_wrong_endpoints(change):
    api=client();api.list_agent_runtime_endpoints.return_value={'runtimeEndpoints':[{**endpoint(),**change}]}
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)
    api.get_agent_runtime.assert_not_called()


@pytest.mark.parametrize('bad',[
    {}, {'customJWTAuthorizer':{}}, {'customJWTAuthorizer':{'allowedWorkloadConfiguration':{}}},
    {'customJWTAuthorizer':{'allowedWorkloadConfiguration':{'hostingEnvironments':[{'arn':GATEWAY},{'arn':GATEWAY+'-other'}]}}},
    {'customJWTAuthorizer':{'allowedWorkloadConfiguration':{'hostingEnvironments':[{'arn':GATEWAY}],'workloadIdentities':['another-workload']}}},
    {'customJWTAuthorizer':{'allowedWorkloadConfiguration':{'hostingEnvironments':[{'arn':'other'}]}}},
])
def test_missing_or_wider_authorization_never_passes(bad):
    api=client();value=runtime();value['authorizerConfiguration']=bad;api.get_agent_runtime.return_value=value
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)


@pytest.mark.parametrize('change',[{'status':'UPDATING'},{'agentRuntimeArn':'other'},{'agentRuntimeId':'other'},{'agentRuntimeVersion':'2'}])
def test_rejects_wrong_runtime_version(change):
    api=client();api.get_agent_runtime.return_value={**runtime(),**change}
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)


def test_rejects_endpoint_changes_during_check():
    api=client();api.list_agent_runtime_endpoints.side_effect=[{'runtimeEndpoints':[endpoint()]},{'runtimeEndpoints':[endpoint(version='2')]}]
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)


def test_pagination_checks_second_page_and_rejects_repeated_tokens():
    api=client();pages=[{'runtimeEndpoints':[endpoint()],'nextToken':'next'}, {'runtimeEndpoints':[endpoint('PROD','2')]}]
    api.list_agent_runtime_endpoints.side_effect=deepcopy(pages)*2
    api.get_agent_runtime.side_effect=lambda agentRuntimeId,agentRuntimeVersion: runtime(agentRuntimeVersion)
    assert check(api,RUNTIME,GATEWAY) is True
    assert api.list_agent_runtime_endpoints.call_args_list[1].kwargs == {'agentRuntimeId':RID,'maxResults':100,'nextToken':'next'}
    assert api.list_agent_runtime_endpoints.call_args_list[3].kwargs == {'agentRuntimeId':RID,'maxResults':100,'nextToken':'next'}
    api=client();api.list_agent_runtime_endpoints.return_value={'runtimeEndpoints':[],'nextToken':'repeat'}
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)


def test_sdk_failures_are_sanitized():
    api=client();api.list_agent_runtime_endpoints.side_effect=RuntimeError('sensitive data')
    with pytest.raises(ValueError,match='^Unable to verify gateway-only runtime configuration$') as error: check(api,RUNTIME,GATEWAY)
    assert 'sensitive data' not in str(error.value)


def test_rejects_wrong_region_before_querying():
    api=client();api.meta.region_name='us-west-2'
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)
    api.list_agent_runtime_endpoints.assert_not_called()


@pytest.mark.parametrize('page',[
    {'runtimeEndpoints':[]},
    {'runtimeEndpoints':None},
    {'runtimeEndpoints':[endpoint(),endpoint()]},
    {'runtimeEndpoints':[endpoint('PROD')]},
    {'runtimeEndpoints':[endpoint()],'nextToken':''},
])
def test_rejects_incomplete_or_ambiguous_endpoint_lists(page):
    api=client();api.list_agent_runtime_endpoints.return_value=page
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)
    api.get_agent_runtime.assert_not_called()


def test_allows_optional_target_version_when_absent():
    api=client();row=endpoint();del row['targetVersion']
    api.list_agent_runtime_endpoints.return_value={'runtimeEndpoints':[row]}
    assert check(api,RUNTIME,GATEWAY) is True


@pytest.mark.parametrize('count,allowed',[(100,True),(101,False)])
def test_endpoint_count_limit(count,allowed):
    api=client();rows=[endpoint()]+[endpoint('A'+str(i)) for i in range(count-1)]
    api.list_agent_runtime_endpoints.return_value={'runtimeEndpoints':rows}
    if allowed:
        assert check(api,RUNTIME,GATEWAY) is True
    else:
        with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)


def test_pagination_limit_does_not_accept_unbounded_empty_pages():
    api=client()
    pages=[{'runtimeEndpoints':[endpoint()],'nextToken':'1'}]
    pages.extend({'runtimeEndpoints':[],'nextToken':str(i)} for i in range(2,12))
    api.list_agent_runtime_endpoints.side_effect=pages
    with pytest.raises(ValueError): check(api,RUNTIME,GATEWAY)
    assert api.list_agent_runtime_endpoints.call_count==10


def test_cli_reports_only_scoped_result_and_uses_sdk_credentials(monkeypatch,capsys):
    api=client();factory=Mock(return_value=api)
    monkeypatch.setattr(check_coa_gateway.boto3,'client',factory)
    monkeypatch.setattr('sys.argv',['check','--region','us-east-1','--runtime-arn',RUNTIME,'--gateway-arn',GATEWAY])
    check_coa_gateway.main()
    factory.assert_called_once_with('bedrock-agentcore-control',region_name='us-east-1')
    assert capsys.readouterr().out=='Gateway-only runtime configuration observed. This does not establish private-data safety.\n'


def test_cli_returns_nonzero_and_sanitizes_sdk_errors(monkeypatch,capsys):
    factory=Mock(side_effect=RuntimeError('sensitive detail'))
    monkeypatch.setattr(check_coa_gateway.boto3,'client',factory)
    monkeypatch.setattr('sys.argv',['check','--region','us-east-1','--runtime-arn',RUNTIME,'--gateway-arn',GATEWAY])
    with pytest.raises(SystemExit) as error: check_coa_gateway.main()
    assert error.value.code==1
    assert capsys.readouterr().err=='Unable to verify gateway-only runtime configuration.\n'
