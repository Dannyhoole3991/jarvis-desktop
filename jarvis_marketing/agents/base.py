"""
What every agent gets: a Context with the store, its business, the adapters, a READ-ONLY view of
the controls, a deadline, and one gate for external actions. Agents return plain data, checked
against their declared OUTPUT schema by the director. They can't change modes, caps, approvals or
their own task's state: they hold no object that could.
"""

import threading

from jarvis_marketing import config


class TaskCancelled(Exception):
    """Deadline passed, the task was cancelled, or the emergency stop went on."""


class SchemaError(ValueError):
    pass


def validate(value, schema, path="output"):
    """schema: a type, a tuple of types, [item_schema] or {"key": schema, "key?": optional schema}."""
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise SchemaError(f"{path} must be an object")
        for key, sub in schema.items():
            optional = key.endswith("?")
            name = key.rstrip("?")
            if name not in value:
                if optional:
                    continue
                raise SchemaError(f"{path}.{name} is missing")
            if optional and value[name] is None:
                continue
            validate(value[name], sub, f"{path}.{name}")
        return value
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise SchemaError(f"{path} must be a list")
        for i, item in enumerate(value):
            validate(item, schema[0], f"{path}[{i}]")
        return value
    types = schema if isinstance(schema, tuple) else (schema,)
    if float in types and int not in types:
        types = types + (int,)
    if isinstance(value, bool) and bool not in types:
        raise SchemaError(f"{path} must be {schema}")
    if not isinstance(value, types):
        raise SchemaError(f"{path} must be {getattr(schema, '__name__', schema)}, not {type(value).__name__}")
    return value


class Context:
    def __init__(self, store, business, business_id, adapters, controls_view, gate, deadline, task_id=None, cancel=None):
        self.store, self.business, self.business_id = store, business, business_id
        self.adapters, self.controls = adapters, controls_view
        self._gate = gate
        self.deadline, self.task_id = deadline, task_id
        self.cancel = cancel or threading.Event()
        self.log_lines = []
        self.limits, self.thresholds = business["limits"], business["thresholds"]
        self.testing, self.economics = business["testing"], business["economics"]
        self.model_version = config.MODEL_VERSION
        self.simulated = adapters.simulated

    def now(self):
        return self.store.clock()

    def check(self):
        if self.cancel.is_set():
            raise TaskCancelled("cancelled")
        if self.now() > self.deadline:
            raise TaskCancelled("timed out")
        if self.controls.stopped():
            raise TaskCancelled("emergency stop")

    def log(self, text):
        self.log_lines.append(str(text)[:500])

    def external(self, action, approval_id=None, content=None, amount=0.0):
        """The ONLY way an agent's work reaches the outside world. Raises PermissionDenied when not allowed."""
        self.check()
        self._gate(action, approval_id=approval_id, content=content, amount=amount)
