"""Release contract for a separately privileged, authenticated deployment service.

The local worker intentionally has no merge/deploy credentials. Receipts must be
verified by an integration before being passed here. This module does not deploy.
"""
from .engine import GateError


class ReleaseController:
    def __init__(self, factory):
        self.factory = factory

    def merged(self, task_id, *, reviewed_commit, merge_commit, pr_url, checks_passed):
        def update(task):
            self.factory._same_commit(task, reviewed_commit)
            if checks_passed is not True or not pr_url.startswith('https://') or not merge_commit:
                raise GateError('Verified PR, merge revision and checks are required')
            task['data']['release'] = {'reviewed_commit': reviewed_commit, 'merge_commit': merge_commit,
                                       'pr_url': pr_url}
            task['state'] = 'MERGED'
        return self.factory._change(task_id, ['RELEASE_READY'], 'merged',
            {'reviewed_commit': reviewed_commit, 'merge_commit': merge_commit, 'pr_url': pr_url}, update)

    def deployed(self, task_id, *, merge_commit, deployment_id):
        def update(task):
            if merge_commit != task['data']['release']['merge_commit'] or not deployment_id:
                raise GateError('Deployment does not match merged revision')
            task['data']['release']['deployment_id'] = deployment_id
            task['state'] = 'OBSERVING'
        return self.factory._change(task_id, ['MERGED'], 'deployed', {'deployment_id': deployment_id}, update)

    def observed(self, task_id, *, deployment_id, healthy, window_complete, evidence):
        def update(task):
            if deployment_id != task['data']['release']['deployment_id'] or not evidence:
                raise GateError('Observation must identify deployment and evidence')
            if type(healthy) is not bool or type(window_complete) is not bool:
                raise GateError('Observation must contain boolean checks')
            if healthy and not window_complete:
                raise GateError('Observation window has not finished')
            task['state'] = 'COMPLETED' if healthy else 'ROLLBACK_REQUIRED'
        return self.factory._change(task_id, ['OBSERVING'], 'observed',
            {'deployment_id': deployment_id, 'healthy': healthy, 'evidence': evidence}, update)

    def rolled_back(self, task_id, *, deployment_id, evidence):
        def update(task):
            if deployment_id != task['data']['release']['deployment_id'] or not evidence:
                raise GateError('Rollback receipt must identify failed deployment and evidence')
            task['state'] = 'ROLLED_BACK'
        return self.factory._change(task_id, ['ROLLBACK_REQUIRED'], 'rolled_back', {'evidence': evidence}, update)
