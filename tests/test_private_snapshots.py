from unittest.mock import Mock, patch
import argparse
import pytest
from scripts import private_snapshots as snapshots


def test_list_only_valid_gitlab_snapshot_keys_across_pages():
    s3=Mock()
    s3.get_paginator.return_value.paginate.return_value=[{'Contents':[{'Key':'private/gitlab/10.json'},{'Key':'private/github/5.json'},{'Key':'private/gitlab/0.json'},{'Key':'private/gitlab/../2.json'}]}, {'Contents':[{'Key':'private/gitlab/2.json'},{'Key':'private/gitlab/01.json'},{'Key':'private/gitlab/3.json.bak'}]}, {}]
    with patch('scripts.private_snapshots.clients',return_value=(s3,Mock())):
        assert snapshots.list_ids() == [2,10]
    s3.get_paginator.assert_called_once_with('list_objects_v2')
    s3.get_paginator.return_value.paginate.assert_called_once_with(Bucket='team-context-records',Prefix='private/gitlab/')
    s3.get_object.assert_not_called()


@pytest.mark.parametrize('value', ['', '0', '-1', '+1', '01', '../1', ' 1', '１', '1'*20])
def test_bad_project_ids_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError,match='^Project ID must be a positive integer$'): snapshots.project_id(value)


def test_project_id_boundaries():
    assert snapshots.project_id('1') == 1
    assert snapshots.project_id('9'*19) == int('9'*19)


def test_remove_targets_only_exact_private_snapshot():
    s3=Mock()
    with patch('scripts.private_snapshots.clients',return_value=(s3,Mock())):
        snapshots.remove(42)
    s3.delete_object.assert_called_once_with(Bucket='team-context-records',Key='private/gitlab/42.json')
    s3.get_paginator.assert_not_called()


@pytest.mark.parametrize('identifier',[None,0,-1,True,'42'])
def test_remove_rejects_invalid_id_before_storage(identifier):
    with patch('scripts.private_snapshots.clients') as clients:
        with pytest.raises(ValueError,match='Invalid project'): snapshots.remove(identifier)
    clients.assert_not_called()


def test_cli_list_and_remove(capsys):
    with patch('scripts.private_snapshots.list_ids',return_value=[2,10]) as list_ids:
        snapshots.main(['list'])
    list_ids.assert_called_once_with()
    assert capsys.readouterr().out == '2\n10\n'
    with patch('scripts.private_snapshots.remove') as remove:
        snapshots.main(['remove','42','--confirm'])
    remove.assert_called_once_with(42)
    assert capsys.readouterr().out == 'Local snapshot removal requested.\n'


@pytest.mark.parametrize('arguments', [[],['remove','42'],['remove','-1','--confirm'],['remove','42','--confirm','unexpected'],['list','--confirm']])
def test_cli_invalid_args_never_access_storage(arguments):
    with patch('scripts.private_snapshots.clients') as clients, patch('scripts.private_snapshots.remove') as remove, patch('scripts.private_snapshots.list_ids') as listing:
        with pytest.raises(SystemExit) as error: snapshots.main(arguments)
        assert error.value.code == 2
        clients.assert_not_called();remove.assert_not_called();listing.assert_not_called()


def test_list_and_remove_propagate_storage_failures():
    s3=Mock()
    s3.get_paginator.side_effect=OSError('failure')
    with patch('scripts.private_snapshots.clients',return_value=(s3,Mock())):
        with pytest.raises(OSError): snapshots.list_ids()
    s3.delete_object.side_effect=OSError('failure')
    with patch('scripts.private_snapshots.clients',return_value=(s3,Mock())):
        with pytest.raises(OSError): snapshots.remove(42)


def test_cli_help_explains_local_scope_and_confirmation(capsys):
    with pytest.raises(SystemExit) as error:
        snapshots.main(['--help'])
    assert error.value.code == 0
    output=capsys.readouterr().out
    assert 'Manage local GitLab private snapshots.' in output
    assert 'List local GitLab project IDs.' in output
    assert 'Remove a local GitLab snapshot.' in output
    with pytest.raises(SystemExit) as error:
        snapshots.main(['remove','--help'])
    assert error.value.code == 0
    assert 'Confirm removal of this local snapshot.' in capsys.readouterr().out
