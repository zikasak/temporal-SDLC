"""Parent of an architecture stage in GET /workflows (FR-125, FR-127): a stage of a task is a
child of the task flow, not of a requirements session."""


def test_architecture_stage_parent_is_its_task_flow(admin):
    response = admin.get('/workflows', params={'type': 'ArchitectureWorkflow'})
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body['detail'], str) and body['detail']
        return
    for item in body['workflows']:
        assert item['workflow_type'] == 'ArchitectureWorkflow', item
        parent = item['parent_workflow_id']
        assert isinstance(parent, str) and parent, item
        assert not parent.startswith('requirements-session-'), item
