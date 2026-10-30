import argparse
import json
import sys
from pathlib import Path
from contextlib import ExitStack

from .contracts import Spec, Build, Review, Verification, Risk
from .engine import Factory, GateError
from .worker import Config, Worker
from .browser import BrowserSession
from .preview import ManagedPreview
from .isolated_browser import DockerBrowserSession
from .operations import Operations


def main():
    parser = argparse.ArgumentParser(description='Software Factory — local control plane')
    parser.add_argument('--db', default='data/factory/state.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit')
    submit.add_argument('--source', choices=['prompt', 'git', 'sentry'], default='prompt')
    submit.add_argument('--event', required=True)
    submit.add_argument('--repo', required=True)
    submit.add_argument('--prompt', required=True)
    for name in ('status', 'events'):
        commands.add_parser(name).add_argument('task_id')
    for name in ('step', 'run'):
        step = commands.add_parser(name)
        step.add_argument('task_id')
        step.add_argument('--config', required=True)
    approve = commands.add_parser('approve')
    approve.add_argument('task_id')
    approve.add_argument('--commit', required=True)
    approve.add_argument('--actor', required=True)
    schema = commands.add_parser('schema')
    schema.add_argument('name', choices=['Spec', 'Build', 'Review', 'Verification', 'Risk'])
    browser = commands.add_parser('browser-check')
    browser.add_argument('--config', required=True)
    browser.add_argument('--commit', required=True)
    usage = commands.add_parser('usage')
    usage.add_argument('task_id')
    recover = commands.add_parser('recover')
    recover.add_argument('task_id')
    recover.add_argument('--config', required=True)
    recover.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'schema':
            result = {c.__name__: c for c in (Spec, Build, Review, Verification, Risk)}[args.name].model_json_schema()
        elif args.command == 'usage':
            result = Operations(args.db).usage(args.task_id)
        elif args.command == 'recover':
            config = Config.model_validate_json(Path(args.config).read_text(encoding='utf-8'))
            result = Worker(config).recover(args.task_id, args.reason)
        elif args.command == 'browser-check':
            config = Config.model_validate_json(Path(args.config).read_text(encoding='utf-8'))
            if config.browser is None:
                raise GateError('Configure browser.base_url first')
            with ExitStack() as stack:
                browser_config = config.browser
                if config.isolation.mode == 'docker':
                    session = stack.enter_context(DockerBrowserSession(config.repo, args.commit,
                        config.preview, config.browser, config.isolation, config.artifacts))
                elif config.preview is not None:
                    preview = stack.enter_context(ManagedPreview(config.repo, args.commit, config.preview,
                        Path(config.artifacts) / 'previews'))
                    browser_config = browser_config.model_copy(update={'base_url': preview.url})
                if config.isolation.mode != 'docker':
                    session = stack.enter_context(BrowserSession(browser_config, args.commit, Path(config.artifacts) / 'browser'))
                session.call('screenshot', {})
                session.call('viewport', {'device': 'mobile'})
                session.call('screenshot', {})
                result = {'transport_ok': session.complete(), 'artifacts': str(session.directory),
                          'note': 'Browser smoke check only; not a UI approval'}
            if not result['transport_ok']:
                raise GateError('Browser smoke check failed; inspect ' + result['artifacts'])
        elif args.command in ('step', 'run'):
            config = Config.model_validate_json(Path(args.config).read_text(encoding='utf-8'))
            worker = Worker(config)
            for _ in range(1 if args.command == 'step' else 20):
                result = worker.step(args.task_id)
                if result['state'] in ('HUMAN_REQUIRED', 'ESCALATED', 'RELEASE_READY'):
                    break
        else:
            factory = Factory(args.db)
            if args.command == 'submit':
                result = factory.submit(args.source, args.event, str(Path(args.repo).resolve()), args.prompt)
            elif args.command == 'approve':
                result = factory.human_approve(args.task_id, args.commit, args.actor)
            else:
                result = factory.get(args.task_id) if args.command == 'status' else factory.events(args.task_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (GateError, ValueError, OSError) as exc:
        print(json.dumps({'blocked': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
