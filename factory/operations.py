"""Persistent reservations and explicit recovery; amounts are integer micro-USD."""
import json
import time
import uuid
from decimal import Decimal, ROUND_CEILING

from pydantic import Field

from .contracts import Contract
from .engine import Factory, GateError


class Price(Contract):
    input_usd_per_million: float = Field(gt=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(gt=0, allow_inf_nan=False)


class Budget(Contract):
    max_calls: int = Field(default=100, ge=1, le=10000)
    max_tokens: int = Field(default=500000, ge=1)
    max_cost_usd: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    max_output_tokens: int = Field(default=8000, ge=1, le=32000)
    max_input_tokens: int = Field(default=100000, ge=1)
    max_recoveries: int = Field(default=3, ge=0, le=20)
    prices: dict[str, Price] = Field(default_factory=dict)


def cost(tokens_in, tokens_out, price):
    return int((Decimal(tokens_in) * Decimal(str(price.input_usd_per_million)) +
                Decimal(tokens_out) * Decimal(str(price.output_usd_per_million))).to_integral_value(rounding=ROUND_CEILING))


class Operations(Factory):
    def __init__(self, path, budget=None):
        super().__init__(path)
        self.budget = budget or Budget()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS api_calls (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, role TEXT NOT NULL,
                    model TEXT NOT NULL, status TEXT NOT NULL, tokens INTEGER NOT NULL,
                    cost INTEGER NOT NULL, price TEXT NOT NULL, usage TEXT, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS executions (
                    task_id TEXT PRIMARY KEY, stage TEXT NOT NULL, attempt INTEGER NOT NULL,
                    status TEXT NOT NULL, started REAL NOT NULL, error_type TEXT);
                CREATE TABLE IF NOT EXISTS recoveries (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(task_id,name));
            ''')

    def require_price(self, model):
        if model not in self.budget.prices:
            raise GateError('Configure a reviewed price for model ' + model)
        return self.budget.prices[model]

    def reserve(self, task_id, role, model, input_tokens, output_tokens):
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
            raise GateError('Invalid token count; no generation allowed')
        price = self.require_price(model)
        if input_tokens > self.budget.max_input_tokens:
            raise GateError('Input token budget exceeded; split the task or context')
        amount = cost(input_tokens, output_tokens, price)
        tokens = input_tokens + output_tokens
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
            if db.execute('SELECT 1 FROM api_calls WHERE task_id=? AND status=?', (task_id, 'reserved')).fetchone():
                raise GateError('Task budget has an unresolved call; recover explicitly')
            total = db.execute('SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(cost),0) FROM api_calls WHERE task_id=?', (task_id,)).fetchone()
            if (total[0] >= self.budget.max_calls or total[1] + tokens > self.budget.max_tokens
                    or total[2] + amount > Decimal(str(self.budget.max_cost_usd)) * 1000000):
                raise GateError('Task budget exhausted; no generation allowed')
            key = uuid.uuid4().hex
            db.execute('INSERT INTO api_calls VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (key, task_id, role, model, 'reserved', tokens, amount, price.model_dump_json(), None, time.time()))
            return key

    def settle(self, key, usage):
        if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens', 'output_tokens')):
            raise GateError('Missing or invalid usage; reservation retained')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM api_calls WHERE id=?', (key,)).fetchone()
            if row is None:
                raise GateError('Unknown usage reservation')
            if row['status'] == 'settled':
                if json.loads(row['usage']) != usage:
                    raise GateError('Conflicting usage settlement')
                return
            price = Price.model_validate_json(row['price'])
            # Cache savings are deliberately not assumed. All input uses the reviewed full rate.
            db.execute('UPDATE api_calls SET status=?,tokens=?,cost=?,usage=? WHERE id=?',
                ('settled', usage['input_tokens'] + usage['output_tokens'],
                 cost(usage['input_tokens'], usage['output_tokens'], price), json.dumps(usage), key))
            exceeded = (usage['input_tokens'] + usage['output_tokens'] > row['tokens'] or
                        cost(usage['input_tokens'], usage['output_tokens'], price) > row['cost'])
        if exceeded:
            raise GateError('Reported usage exceeded reservation; recorded actual usage, execution blocked')

    def usage(self, task_id):
        self.get(task_id)
        with self._db() as db:
            calls = [dict(r) for r in db.execute('SELECT id,role,model,status,tokens,cost,usage,at FROM api_calls WHERE task_id=? ORDER BY at', (task_id,))]
            execution = db.execute('SELECT * FROM executions WHERE task_id=?', (task_id,)).fetchone()
            recoveries = [dict(r) for r in db.execute('SELECT reason,at FROM recoveries WHERE task_id=? ORDER BY id', (task_id,))]
        return {'task_id': task_id, 'tokens': sum(r['tokens'] for r in calls),
                'cost_is_estimate': True,
                'cost_micro_usd': sum(r['cost'] for r in calls), 'calls': calls,
                'execution': dict(execution) if execution else None, 'recoveries': recoveries}

    def begin(self, task_id, stage, attempt):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM executions WHERE task_id=?', (task_id,)).fetchone()
            if row and row['stage'] == stage and row['attempt'] == attempt and row['status'] in ('running', 'interrupted'):
                raise GateError('Execution interrupted or active; inspect usage and run recover explicitly')
            db.execute('INSERT OR REPLACE INTO executions VALUES (?,?,?,?,?,?)',
                       (task_id, stage, attempt, 'running', time.time(), None))

    def finish(self, task_id, error=None):
        with self._db() as db:
            db.execute('UPDATE executions SET status=?,error_type=? WHERE task_id=?',
                       ('interrupted' if error else 'complete', type(error).__name__ if error else None, task_id))

    def recover(self, task_id, reason):
        if not reason.strip() or len(reason) > 2000:
            raise GateError('Recovery requires a bounded operator reason')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
            count = db.execute('SELECT COUNT(*) FROM recoveries WHERE task_id=?', (task_id,)).fetchone()[0]
            execution = db.execute('SELECT status FROM executions WHERE task_id=?', (task_id,)).fetchone()
            reserved = db.execute('SELECT 1 FROM api_calls WHERE task_id=? AND status=?', (task_id, 'reserved')).fetchone()
            if not reserved and (not execution or execution['status'] not in ('running', 'interrupted')):
                raise GateError('No interrupted execution to recover')
            if count >= self.budget.max_recoveries:
                raise GateError('Recovery limit reached')
            db.execute('UPDATE api_calls SET status=? WHERE task_id=? AND status=?', ('unknown', task_id, 'reserved'))
            db.execute('UPDATE executions SET status=? WHERE task_id=?', ('recoverable', task_id))
            db.execute('INSERT INTO recoveries(task_id,reason,at) VALUES (?,?,?)', (task_id, reason, time.time()))
        return self.usage(task_id)

    def checkpoint(self, task_id, name, value=None):
        with self._db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)', (task_id, name, json.dumps(value)))
            row = db.execute('SELECT value FROM checkpoints WHERE task_id=? AND name=?', (task_id, name)).fetchone()
            return json.loads(row['value']) if row else None
