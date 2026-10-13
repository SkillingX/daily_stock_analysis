"""Security regressions for fork PR review workflow event/permission boundaries.
Run: python -m pytest tests/test_pr_review_workflow.py
Reads actual workflow configuration; GitHub execution still needs remote acceptance.
"""
from pathlib import Path
import yaml


def test_privileged_pr_metadata_jobs_do_not_checkout_or_execute_pr_code():
    workflow = yaml.load(Path('.github/workflows/pr-review.yml').read_text(), Loader=yaml.BaseLoader)
    for job in workflow['jobs'].values():
        if "github.event_name == 'pull_request_target'" in job.get('if', ''):
            for step in job['steps']:
                if 'actions/checkout' in step.get('uses', ''):
                    assert step['with']['ref'] == '${{ github.event.pull_request.base.sha }}'
            if job.get('permissions', {}).get('issues') == 'write':
                assert all('actions/checkout' not in step.get('uses', '') for step in job['steps'])
                assert all('run' not in step for step in job['steps'])
            else:
                assert 'head.repo.full_name == github.repository' in job['if']
                assert job.get('permissions', {}).get('contents') == 'read'
        elif any('actions/checkout' in step.get('uses', '') for step in job['steps']):
            assert "github.event_name != 'pull_request_target'" in job.get('if', '')
            assert job.get('permissions', {}).get('contents') == 'read'
            assert 'write' not in job.get('permissions', {}).values()
    assert 'pull_request' in workflow['on']
    assert 'pull_request_target' in workflow['on']
    assert 'allow-unsafe-pr-checkout' not in Path('.github/workflows/pr-review.yml').read_text()


def test_metadata_handler_ignores_stale_events_and_treats_filenames_as_data():
    import json
    import subprocess
    import shutil
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the actual github-script handler')
    workflow = yaml.load(Path('.github/workflows/pr-review.yml').read_text(), Loader=yaml.BaseLoader)
    script = workflow['jobs']['metadata']['steps'][0]['with']['script']
    for current, expected_writes in [('new-sha', 0), ('expected-sha', 2)]:
        harness = '''
        const writes = [];
        const context = {repo: {owner:'fixture',repo:'fixture'},issue:{number:1}};
        process.env.EXPECTED_HEAD_SHA='expected-sha';
        const github={rest:{pulls:{get:async()=>({data:{head:{sha:CURRENT}}}),listFiles:'files'},issues:{listLabelsForRepo:'labels',listComments:'comments',addLabels:async(args)=>writes.push(args),createComment:async(args)=>writes.push(args),updateComment:async(args)=>writes.push(args)}}};
        github.paginate=async(method)=>method==='files'?[{filename:'data_provider/$(touch SHOULD_NOT_EXIST).py'}]:method==='labels'?[{name:'data-source'}]:[];
        (async()=>{SCRIPT})().then(()=>process.stdout.write(JSON.stringify(writes)));
        '''.replace('CURRENT', json.dumps(current)).replace('SCRIPT', script)
        result = json.loads(subprocess.check_output([node, '-e', harness], text=True))
        assert len(result) == expected_writes
        if result:
            assert result[0]['labels'] == ['data-source']
            assert '未执行，不代表通过' in result[1]['body']
            assert '$(touch' not in result[1]['body']
