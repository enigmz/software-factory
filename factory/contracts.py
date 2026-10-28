from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r'\S')]
SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Task(Contract):
    id: Annotated[str, Field(pattern=r'^[a-z0-9_-]{1,80}$')]
    description: Text
    acceptance: list[Text] = Field(min_length=1)
    validation: Literal['tdd', 'visual']


class Spec(Contract):
    objective: Text
    acceptance: list[Text] = Field(min_length=1)
    exclusions: list[Text]
    tasks: list[Task] = Field(min_length=1, max_length=50)
    rollback: Text
    brain_revision: SHA

    @model_validator(mode='after')
    def unique_tasks(self):
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError('Task IDs must be unique')
        return self


class Build(Contract):
    commit: SHA
    base_commit: SHA
    brain_revision: SHA
    changed_files: list[Text] = Field(min_length=1)
    summary: Text


class Verification(Contract):
    commit: SHA
    checks: dict[str, bool]
    evidence: list[Text] = Field(min_length=1)
    task_evidence: dict[str, list[Text]]


class Review(Contract):
    role: Literal['security', 'ux', 'ui', 'consistency']
    commit: SHA
    verdict: Literal['PASS', 'FAIL', 'BLOCKED', 'NOT_APPLICABLE']
    summary: Text
    evidence: list[Text] = Field(min_length=1)


class Risk(Contract):
    commit: SHA
    level: Literal['low', 'medium', 'high']
    reasons: list[Text] = Field(min_length=1)
    reversible: bool
    uncertain: bool
