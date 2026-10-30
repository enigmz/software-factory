from contextlib import contextmanager

from .browser import BrowserHarness
from .preview import ManagedPreview


class ManagedBrowserHarness:
    """Start only for visual tasks/UI reviews; never for spec, build or risk calls."""
    def __init__(self, repo, preview_config, browser_config, artifacts, agent_factory):
        self.repo = repo
        self.preview_config, self.browser_config = preview_config, browser_config
        self.artifacts, self.agent_factory = artifacts, agent_factory

    @contextmanager
    def harness(self, commit):
        with ManagedPreview(self.repo, commit, self.preview_config, self.artifacts / 'previews') as preview:
            config = self.browser_config.model_copy(update={'base_url': preview.url})
            yield BrowserHarness(config, self.artifacts / 'browser', self.agent_factory)

    def verify_task(self, commit, task):
        with self.harness(commit) as browser:
            return browser.verify_task(commit, task)

    def review(self, agent, context, path):
        with self.harness(context['commit']) as browser:
            return browser.review(agent, context, path)
