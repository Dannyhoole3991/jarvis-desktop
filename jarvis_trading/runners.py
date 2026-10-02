"""
Routes each strategy to the engine that runs it: Freqtrade for JT_* strategies,
the lab's own walk-forward simulator for ML_* strategies. Both expose the same
interface the Lab expects, and both go through the identical gates.
"""

from . import freqtrade_adapter as A


class RouterRunner:
    def __init__(self, ft=A, ml=None):
        self.ft = ft
        self._ml = ml

    @property
    def ml(self):
        if self._ml is None:
            from .mlrunner import MLRunner  # imported lazily: needs scikit-learn
            self._ml = MLRunner()
        return self._ml

    def _engine(self, name):
        return self.ml if name.startswith("ML_") else self.ft

    def write_strategy(self, name, source):
        return self._engine(name).write_strategy(name, source)

    def run_backtest(self, name, timerange, timeframe, pairs=None, **kw):
        return self._engine(name).run_backtest(name, timerange, timeframe, pairs=pairs, **kw)

    def run_lookahead_analysis(self, name, timerange, timeframe, pairs=None, **kw):
        return self._engine(name).run_lookahead_analysis(name, timerange, timeframe, pairs=pairs, **kw)

    def tool_versions(self):
        versions = dict(self.ft.tool_versions())
        if self._ml is not None:
            versions.update(self._ml.tool_versions())
        return versions

    def data_hash(self, pairs, timeframe):
        return self.ft.data_hash(pairs, timeframe)
