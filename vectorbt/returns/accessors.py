"""Custom pandas accessors.

Methods can be accessed as follows:

* `ReturnsSRAccessor` -> `pd.Series.vbt.returns.*`
* `ReturnsDFAccessor` -> `pd.DataFrame.vbt.returns.*`

!!! note
    The underlying Series/DataFrame must already be a return series.
    To convert price to returns, use `ReturnsAccessor.from_value`.

There are three options to compute returns and get the accessor:

```python-repl
>>> import numpy as np
>>> import pandas as pd
>>> import vectorbt as vbt

>>> price = pd.Series([1.1, 1.2, 1.3, 1.2, 1.1])

>>> # 1. pd.Series.pct_change
>>> rets = price.pct_change()
>>> ret_acc = rets.vbt.returns(freq='d')

>>> # 2. vectorbt.generic.accessors.GenericAccessor.to_returns
>>> rets = price.vbt.to_returns()
>>> ret_acc = rets.vbt.returns(freq='d')

>>> # 3. vectorbt.returns.accessors.ReturnsAccessor.from_value
>>> ret_acc = pd.Series.vbt.returns.from_value(price, freq='d')

>>> # vectorbt.returns.accessors.ReturnsAccessor.total
>>> ret_acc.total()
0.0
```

The accessors extend `vectorbt.generic.accessors`.

```python-repl
>>> # inherited from GenericAccessor
>>> ret_acc.max()
0.09090909090909083
```

## Defaults

`vectorbt.returns.accessors.ReturnsAccessor` accepts `defaults` dictionary where you can pass
defaults for arguments used throughout the accessor, such as

* `start_value`: The starting returns.
* `window`: Window length.
* `minp`: Minimum number of observations in window required to have a value.
* `ddof`: Means Delta Degrees of Freedom.
* `risk_free`: Constant risk-free return throughout the period.
* `levy_alpha`: Scaling relation (Levy stability exponent).
* `required_return`: Minimum acceptance return of the investor.
* `cutoff`: Decimal representing the percentage cutoff for the bottom percentile of returns.

## Stats

!!! hint
    For details on `ReturnsAccessor.stats`, see `vectorbt.generic.stats_builder.StatsBuilderMixin.stats`.

    Also see `vectorbt.portfolio.base` for more examples.

```python-repl
>>> ret_acc.stats()
UserWarning: Metric 'benchmark_return' requires benchmark_rets to be set
UserWarning: Metric 'alpha' requires benchmark_rets to be set
UserWarning: Metric 'beta' requires benchmark_rets to be set

Start                                      0
End                                        4
Duration                     5 days 00:00:00
Total Return [%]                           0
Annualized Return [%]                      0
Annualized Volatility [%]            184.643
Sharpe Ratio                        0.691185
Calmar Ratio                               0
Max Drawdown [%]                     15.3846
Omega Ratio                          1.08727
Sortino Ratio                        1.17805
Skew                              0.00151002
Kurtosis                            -5.94737
Tail Ratio                           1.08985
Common Sense Ratio                   1.08985
Value at Risk                     -0.0823718
dtype: object
```

The missing `benchmark_rets` can be passed inside of `settings`:

```python-repl
>>> benchmark = pd.Series([1.05, 1.1, 1.15, 1.1, 1.05])
>>> benchmark_rets = benchmark.vbt.to_returns()

>>> ret_acc.stats(settings=dict(benchmark_rets=benchmark_rets))
Start                                      0
End                                        4
Duration                     5 days 00:00:00
Total Return [%]                           0
Benchmark Return [%]                       0
Annualized Return [%]                      0
Annualized Volatility [%]            184.643
Sharpe Ratio                        0.691185
Calmar Ratio                               0
Max Drawdown [%]                     15.3846
Omega Ratio                          1.08727
Sortino Ratio                        1.17805
Skew                              0.00151002
Kurtosis                            -5.94737
Tail Ratio                           1.08985
Common Sense Ratio                   1.08985
Value at Risk                     -0.0823718
Alpha                                0.78789
Beta                                 1.83864
dtype: object
```
"""

import numpy as np
import pandas as pd
from scipy.stats import skew, kurtosis
import warnings

from vectorbt import _typing as tp
from vectorbt.root_accessors import register_dataframe_accessor, register_series_accessor
from vectorbt.utils import checks
from vectorbt.utils.config import merge_dicts, Config
from vectorbt.utils.figure import make_figure, get_domain
from vectorbt.utils.decorators import cached_property, cached_method
from vectorbt.utils.datetime import freq_to_timedelta, DatetimeIndexes
from vectorbt.utils.attr import AttrResolverT
from vectorbt.base.reshape_fns import to_1d, to_2d, broadcast, broadcast_to
from vectorbt.base.array_wrapper import Wrapping
from vectorbt.generic.drawdowns import Drawdowns
from vectorbt.generic.accessors import (
    GenericAccessor,
    GenericSRAccessor,
    GenericDFAccessor
)
from vectorbt.generic.stats_builder import StatsBuilderMixin
from vectorbt.returns import nb, metrics

ReturnsAccessorT = tp.TypeVar("ReturnsAccessorT", bound="ReturnsAccessor")


class ReturnsAccessor(GenericAccessor):
    """Accessor on top of return series. For both, Series and DataFrames.

    Accessible through `pd.Series.vbt.returns` and `pd.DataFrame.vbt.returns`.

    Args:
        obj (pd.Series or pd.DataFrame): Pandas object.
        year_freq (any): Year frequency for annualization purposes.
        defaults (dict): Defaults that override `returns.defaults` in `vectorbt._settings.settings`.
        **kwargs: Keyword arguments that are passed down to `vectorbt.generic.accessors.GenericAccessor`."""

    def __init__(self,
                 obj: tp.SeriesFrame,
                 year_freq: tp.Optional[tp.FrequencyLike] = None,
                 defaults: tp.KwargsLike = None,
                 **kwargs) -> None:
        if not checks.is_pandas(obj):  # parent accessor
            obj = obj._obj

        # Set defaults
        self._year_freq = year_freq
        self._defaults = defaults

        GenericAccessor.__init__(self, obj, **kwargs)

    @property
    def sr_accessor_cls(self):
        """Accessor class for `pd.Series`."""
        return ReturnsSRAccessor

    @property
    def df_accessor_cls(self):
        """Accessor class for `pd.DataFrame`."""
        return ReturnsDFAccessor

    @classmethod
    def from_value(cls: tp.Type[ReturnsAccessorT],
                   value: tp.SeriesFrame,
                   init_value: tp.MaybeSeries = np.nan,
                   broadcast_kwargs: tp.KwargsLike = None,
                   wrap_kwargs: tp.KwargsLike = None,
                   **kwargs) -> ReturnsAccessorT:
        """Returns a new `ReturnsAccessor` instance with returns calculated from `value`."""
        if broadcast_kwargs is None:
            broadcast_kwargs = {}
        if wrap_kwargs is None:
            wrap_kwargs = {}
        if not checks.is_any_array(value):
            value = np.asarray(value)
        value_2d = to_2d(value, raw=True)
        init_value = broadcast(init_value, to_shape=value_2d.shape[1], **broadcast_kwargs)

        returns = nb.returns_nb(value_2d, init_value)
        returns = value.vbt.wrapper.wrap(returns, **wrap_kwargs)
        return cls(returns, **kwargs)

    @property
    def year_freq(self) -> tp.Optional[pd.Timedelta]:
        """Year frequency for annualization purposes."""
        if self._year_freq is None:
            from vectorbt._settings import settings
            returns_cfg = settings['returns']

            year_freq = returns_cfg['year_freq']
            if year_freq is None:
                return None
            return freq_to_timedelta(year_freq)
        return freq_to_timedelta(self._year_freq)

    @property
    def ann_factor(self) -> float:
        """Get annualization factor."""
        if self.wrapper.freq is None:
            raise ValueError("Index frequency is None. "
                             "Pass it as `freq` or define it globally under `settings.array_wrapper`.")
        if self.year_freq is None:
            raise ValueError("Year frequency is None. "
                             "Pass `year_freq` or define it globally under `settings.returns`.")
        return self.year_freq / self.wrapper.freq

    @property
    def defaults(self) -> tp.Kwargs:
        """Defaults for `ReturnsAccessor`.

        Merges `returns.defaults` in `vectorbt._settings.settings` with `defaults` from `ReturnsAccessor.__init__`."""
        from vectorbt._settings import settings
        returns_defaults_cfg = settings['returns']['defaults']

        return merge_dicts(
            returns_defaults_cfg,
            self._defaults
        )

    def daily(self, **kwargs) -> tp.SeriesFrame:
        """Daily returns."""
        checks.assert_type(self.wrapper.index, DatetimeIndexes)

        if self.wrapper.freq == pd.Timedelta('1D'):
            return self.obj
        return self.resample_apply('1D', nb.total_return_apply_nb, **kwargs)

    def annual(self, **kwargs) -> tp.SeriesFrame:
        """Annual returns."""
        checks.assert_type(self.obj.index, DatetimeIndexes)

        if self.wrapper.freq == self.year_freq:
            return self.obj
        return self.resample_apply(self.year_freq, nb.total_return_apply_nb, **kwargs)

    def cumulative(self,
                   start_value: tp.Optional[float] = None,
                   wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Cumulative returns."""
        if start_value is None:
            start_value = self.defaults['start_value']
        cumulative = nb.cum_returns_nb(self.to_2d_array(), start_value)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(cumulative, **wrap_kwargs)

    def total(self, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Total return."""
        result = nb.cum_returns_final_nb(self.to_2d_array(), 0.)
        wrap_kwargs = merge_dicts(dict(name_or_index='total_return'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_total(self,
                      window: tp.Optional[int] = None,
                      minp: tp.Optional[int] = None,
                      wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.total`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        result = nb.rolling_cum_returns_final_nb(self.to_2d_array(), window, minp, 0.)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def annualized(self, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Mean annual growth rate of returns.

        This is equivalent to the compound annual growth rate."""
        result = nb.annualized_return_nb(self.to_2d_array(), self.ann_factor)
        wrap_kwargs = merge_dicts(dict(name_or_index='annualized_return'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_annualized(self,
                           window: tp.Optional[int] = None,
                           minp: tp.Optional[int] = None,
                           wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.annualized`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        result = nb.rolling_annualized_return_nb(self.to_2d_array(), window, minp, self.ann_factor)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def annualized_volatility(self,
                              levy_alpha: tp.Optional[float] = None,
                              ddof: tp.Optional[int] = None,
                              wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Annualized volatility of a strategy."""
        if levy_alpha is None:
            levy_alpha = self.defaults['levy_alpha']
        if ddof is None:
            ddof = self.defaults['ddof']
        result = nb.annualized_volatility_nb(self.to_2d_array(), self.ann_factor, levy_alpha, ddof)
        wrap_kwargs = merge_dicts(dict(name_or_index='annualized_volatility'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_annualized_volatility(self,
                                      window: tp.Optional[int] = None,
                                      minp: tp.Optional[int] = None,
                                      levy_alpha: tp.Optional[float] = None,
                                      ddof: tp.Optional[int] = None,
                                      wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.annualized_volatility`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if levy_alpha is None:
            levy_alpha = self.defaults['levy_alpha']
        if ddof is None:
            ddof = self.defaults['ddof']
        result = nb.rolling_annualized_volatility_nb(
            self.to_2d_array(), window, minp, self.ann_factor, levy_alpha, ddof)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def calmar_ratio(self, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Calmar ratio, or drawdown ratio, of a strategy."""
        result = nb.calmar_ratio_nb(self.to_2d_array(), self.ann_factor)
        wrap_kwargs = merge_dicts(dict(name_or_index='calmar_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_calmar_ratio(self,
                             window: tp.Optional[int] = None,
                             minp: tp.Optional[int] = None,
                             wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.calmar_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        result = nb.rolling_calmar_ratio_nb(self.to_2d_array(), window, minp, self.ann_factor)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def omega_ratio(self,
                    risk_free: tp.Optional[float] = None,
                    required_return: tp.Optional[float] = None,
                    wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Omega ratio of a strategy."""
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        if required_return is None:
            required_return = self.defaults['required_return']
        result = nb.omega_ratio_nb(self.to_2d_array(), self.ann_factor, risk_free, required_return)
        wrap_kwargs = merge_dicts(dict(name_or_index='omega_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_omega_ratio(self,
                            window: tp.Optional[int] = None,
                            minp: tp.Optional[int] = None,
                            risk_free: tp.Optional[float] = None,
                            required_return: tp.Optional[float] = None,
                            wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.omega_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        if required_return is None:
            required_return = self.defaults['required_return']
        result = nb.rolling_omega_ratio_nb(
            self.to_2d_array(), window, minp, self.ann_factor, risk_free, required_return)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def sharpe_ratio(self,
                     risk_free: tp.Optional[float] = None,
                     ddof: tp.Optional[int] = None,
                     wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Sharpe ratio of a strategy."""
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        if ddof is None:
            ddof = self.defaults['ddof']
        result = nb.sharpe_ratio_nb(self.to_2d_array(), self.ann_factor, risk_free, ddof)
        wrap_kwargs = merge_dicts(dict(name_or_index='sharpe_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_sharpe_ratio(self,
                             window: tp.Optional[int] = None,
                             minp: tp.Optional[int] = None,
                             risk_free: tp.Optional[float] = None,
                             ddof: tp.Optional[int] = None,
                             wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.sharpe_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        if ddof is None:
            ddof = self.defaults['ddof']
        result = nb.rolling_sharpe_ratio_nb(self.to_2d_array(), window, minp, self.ann_factor, risk_free, ddof)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def deflated_sharpe_ratio(self,
                              risk_free: tp.Optional[float] = None,
                              ddof: tp.Optional[int] = None,
                              var_sharpe: tp.Optional[float] = None,
                              nb_trials: tp.Optional[int] = None,
                              bias: bool = True,
                              wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Deflated Sharpe Ratio (DSR).

        Expresses the chance that the advertised strategy has a positive Sharpe ratio.

        If `var_sharpe` is None, is calculated based on all columns.
        If `nb_trials` is None, is set to the number of columns."""
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        if ddof is None:
            ddof = self.defaults['ddof']
        sharpe_ratio = to_1d(self.sharpe_ratio(risk_free=risk_free), raw=True)
        if var_sharpe is None:
            var_sharpe = np.var(sharpe_ratio, ddof=ddof)
        if nb_trials is None:
            nb_trials = self.wrapper.shape_2d[1]
        returns = to_2d(self.obj, raw=True)
        nanmask = np.isnan(returns)
        if nanmask.any():
            returns = returns.copy()
            returns[nanmask] = 0.
        result = metrics.deflated_sharpe_ratio(
            est_sharpe=sharpe_ratio / np.sqrt(self.ann_factor),
            var_sharpe=var_sharpe / self.ann_factor,
            nb_trials=nb_trials,
            backtest_horizon=self.wrapper.shape_2d[0],
            skew=skew(returns, axis=0, bias=bias),
            kurtosis=kurtosis(returns, axis=0, bias=bias)
        )
        wrap_kwargs = merge_dicts(dict(name_or_index='deflated_sharpe_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def downside_risk(self,
                      required_return: tp.Optional[float] = None,
                      wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Downside deviation below a threshold."""
        if required_return is None:
            required_return = self.defaults['required_return']
        result = nb.downside_risk_nb(self.to_2d_array(), self.ann_factor, required_return)
        wrap_kwargs = merge_dicts(dict(name_or_index='downside_risk'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_downside_risk(self,
                              window: tp.Optional[int] = None,
                              minp: tp.Optional[int] = None,
                              required_return: tp.Optional[float] = None,
                              wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.downside_risk`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if required_return is None:
            required_return = self.defaults['required_return']
        result = nb.rolling_downside_risk_nb(self.to_2d_array(), window, minp, self.ann_factor, required_return)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def sortino_ratio(self,
                      required_return: tp.Optional[float] = None,
                      wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Sortino ratio of a strategy."""
        if required_return is None:
            required_return = self.defaults['required_return']
        result = nb.sortino_ratio_nb(self.to_2d_array(), self.ann_factor, required_return)
        wrap_kwargs = merge_dicts(dict(name_or_index='sortino_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_sortino_ratio(self,
                              window: tp.Optional[int] = None,
                              minp: tp.Optional[int] = None,
                              required_return: tp.Optional[float] = None,
                              wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.sortino_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if required_return is None:
            required_return = self.defaults['required_return']
        result = nb.rolling_sortino_ratio_nb(self.to_2d_array(), window, minp, self.ann_factor, required_return)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def information_ratio(self,
                          benchmark_rets: tp.ArrayLike,
                          ddof: tp.Optional[int] = None,
                          wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Information ratio of a strategy."""
        if ddof is None:
            ddof = self.defaults['ddof']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.information_ratio_nb(self.to_2d_array(), benchmark_rets, ddof)
        wrap_kwargs = merge_dicts(dict(name_or_index='information_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_information_ratio(self,
                                  benchmark_rets: tp.ArrayLike,
                                  window: tp.Optional[int] = None,
                                  minp: tp.Optional[int] = None,
                                  ddof: tp.Optional[int] = None,
                                  wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.information_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if ddof is None:
            ddof = self.defaults['ddof']
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.rolling_information_ratio_nb(self.to_2d_array(), window, minp, benchmark_rets, ddof)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def beta(self, benchmark_rets: tp.ArrayLike, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Beta."""
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.beta_nb(self.to_2d_array(), benchmark_rets)
        wrap_kwargs = merge_dicts(dict(name_or_index='beta'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_beta(self,
                     benchmark_rets: tp.ArrayLike,
                     window: tp.Optional[int] = None,
                     minp: tp.Optional[int] = None,
                     wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.beta`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.rolling_beta_nb(self.to_2d_array(), window, minp, benchmark_rets)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def alpha(self,
              benchmark_rets: tp.ArrayLike,
              risk_free: tp.Optional[float] = None,
              wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Annualized alpha."""
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.alpha_nb(self.to_2d_array(), benchmark_rets, self.ann_factor, risk_free)
        wrap_kwargs = merge_dicts(dict(name_or_index='alpha'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_alpha(self,
                      benchmark_rets: tp.ArrayLike,
                      window: tp.Optional[int] = None,
                      minp: tp.Optional[int] = None,
                      risk_free: tp.Optional[float] = None,
                      wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.alpha`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if risk_free is None:
            risk_free = self.defaults['risk_free']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.rolling_alpha_nb(self.to_2d_array(), window, minp, benchmark_rets, self.ann_factor, risk_free)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def tail_ratio(self, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Ratio between the right (95%) and left tail (5%)."""
        result = nb.tail_ratio_nb(self.to_2d_array())
        wrap_kwargs = merge_dicts(dict(name_or_index='tail_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_tail_ratio(self,
                           window: tp.Optional[int] = None,
                           minp: tp.Optional[int] = None,
                           wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.tail_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        result = nb.rolling_tail_ratio_nb(self.to_2d_array(), window, minp)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def common_sense_ratio(self, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Common Sense Ratio."""
        result = to_1d(self.tail_ratio(), raw=True) * (1 + to_1d(self.annualized(), raw=True))
        wrap_kwargs = merge_dicts(dict(name_or_index='common_sense_ratio'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_common_sense_ratio(self,
                                   window: tp.Optional[int] = None,
                                   minp: tp.Optional[int] = None,
                                   wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.common_sense_ratio`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        rolling_tail_ratio = to_2d(self.rolling_tail_ratio(window, minp=minp), raw=True)
        rolling_annualized = to_2d(self.rolling_annualized(window, minp=minp), raw=True)
        result = rolling_tail_ratio * (1 + rolling_annualized)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def value_at_risk(self,
                      cutoff: tp.Optional[float] = None,
                      wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Value at risk (VaR) of a returns stream."""
        if cutoff is None:
            cutoff = self.defaults['cutoff']
        result = nb.value_at_risk_nb(self.to_2d_array(), cutoff)
        wrap_kwargs = merge_dicts(dict(name_or_index='value_at_risk'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_value_at_risk(self,
                              window: tp.Optional[int] = None,
                              minp: tp.Optional[int] = None,
                              cutoff: tp.Optional[float] = None,
                              wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.value_at_risk`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if cutoff is None:
            cutoff = self.defaults['cutoff']
        result = nb.rolling_value_at_risk_nb(self.to_2d_array(), window, minp, cutoff)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def cond_value_at_risk(self,
                           cutoff: tp.Optional[float] = None,
                           wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Conditional value at risk (CVaR) of a returns stream."""
        if cutoff is None:
            cutoff = self.defaults['cutoff']
        result = nb.cond_value_at_risk_nb(self.to_2d_array(), cutoff)
        wrap_kwargs = merge_dicts(dict(name_or_index='cond_value_at_risk'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_cond_value_at_risk(self,
                                   window: tp.Optional[int] = None,
                                   minp: tp.Optional[int] = None,
                                   cutoff: tp.Optional[float] = None,
                                   wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.cond_value_at_risk`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        if cutoff is None:
            cutoff = self.defaults['cutoff']
        result = nb.rolling_cond_value_at_risk_nb(self.to_2d_array(), window, minp, cutoff)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def capture(self, benchmark_rets: tp.ArrayLike, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Capture ratio."""
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.capture_nb(self.to_2d_array(), benchmark_rets, self.ann_factor)
        wrap_kwargs = merge_dicts(dict(name_or_index='capture'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_capture(self,
                        benchmark_rets: tp.ArrayLike,
                        window: tp.Optional[int] = None,
                        minp: tp.Optional[int] = None,
                        wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.capture`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.rolling_capture_nb(self.to_2d_array(), window, minp, benchmark_rets, self.ann_factor)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def up_capture(self, benchmark_rets: tp.ArrayLike, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Capture ratio for periods when the benchmark return is positive."""
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.up_capture_nb(self.to_2d_array(), benchmark_rets, self.ann_factor)
        wrap_kwargs = merge_dicts(dict(name_or_index='up_capture'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_up_capture(self,
                           benchmark_rets: tp.ArrayLike,
                           window: tp.Optional[int] = None,
                           minp: tp.Optional[int] = None,
                           wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.up_capture`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.rolling_up_capture_nb(self.to_2d_array(), window, minp, benchmark_rets, self.ann_factor)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def down_capture(self, benchmark_rets: tp.ArrayLike, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Capture ratio for periods when the benchmark return is negative."""
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.down_capture_nb(self.to_2d_array(), benchmark_rets, self.ann_factor)
        wrap_kwargs = merge_dicts(dict(name_or_index='down_capture'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_down_capture(self,
                             benchmark_rets: tp.ArrayLike,
                             window: tp.Optional[int] = None,
                             minp: tp.Optional[int] = None,
                             wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.down_capture`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        benchmark_rets = broadcast_to(to_2d(benchmark_rets, raw=True), to_2d(self.obj, raw=True))
        result = nb.rolling_down_capture_nb(self.to_2d_array(), window, minp, benchmark_rets, self.ann_factor)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def drawdown(self, wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Relative decline from a peak."""
        result = nb.drawdown_nb(self.to_2d_array())
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    def max_drawdown(self, wrap_kwargs: tp.KwargsLike = None) -> tp.MaybeSeries:
        """Total maximum drawdown (MDD)."""
        result = nb.max_drawdown_nb(self.to_2d_array())
        wrap_kwargs = merge_dicts(dict(name_or_index='max_drawdown'), wrap_kwargs)
        return self.wrapper.wrap_reduced(result, **wrap_kwargs)

    def rolling_max_drawdown(self,
                             window: tp.Optional[int] = None,
                             minp: tp.Optional[int] = None,
                             wrap_kwargs: tp.KwargsLike = None) -> tp.SeriesFrame:
        """Rolling version of `ReturnsAccessor.max_drawdown`."""
        if window is None:
            window = self.defaults['window']
        if minp is None:
            minp = self.defaults['minp']
        result = nb.rolling_max_drawdown_nb(self.to_2d_array(), window, minp)
        wrap_kwargs = merge_dicts({}, wrap_kwargs)
        return self.wrapper.wrap(result, **wrap_kwargs)

    @cached_property
    def drawdowns(self) -> Drawdowns:
        """`ReturnsAccessor.get_drawdowns` with default arguments."""
        return self.get_drawdowns()

    @cached_method
    def get_drawdowns(self, group_by: tp.GroupByLike = None, **kwargs) -> Drawdowns:
        """Generate drawdown records of cumulative returns.

        See `vectorbt.generic.drawdowns.Drawdowns`."""
        if group_by is None:
            group_by = self.wrapper.grouper.group_by
        return self.cumulative(start_value=1.).vbt(freq=self.wrapper.freq, group_by=group_by).get_drawdowns(**kwargs)

    def resolve_self(self: AttrResolverT,
                     cond_kwargs: tp.KwargsLike = None,
                     custom_arg_names: tp.Optional[tp.Set[str]] = None,
                     impacts_caching: bool = True,
                     silence_warnings: bool = False) -> AttrResolverT:
        """Resolve self.

        Creates a copy of this instance if a different `year` or `year_freq` can be found in `cond_kwargs`."""
        if cond_kwargs is None:
            cond_kwargs = {}
        if custom_arg_names is None:
            custom_arg_names = set()

        reself = Wrapping.resolve_self(
            self,
            cond_kwargs=cond_kwargs,
            custom_arg_names=custom_arg_names,
            impacts_caching=impacts_caching,
            silence_warnings=silence_warnings
        )
        if 'year_freq' in cond_kwargs:
            self_copy = reself.copy(year_freq=cond_kwargs['year_freq'])

            if self_copy.year_freq != reself.year_freq:
                if not silence_warnings:
                    warnings.warn(f"Changing the year frequency will create a copy of this object. "
                                  f"Consider setting it upon the creation to re-use cache.", stacklevel=2)
                for alias in reself.self_aliases:
                    if alias not in custom_arg_names:
                        cond_kwargs[alias] = self_copy
                cond_kwargs['year_freq'] = self_copy.year_freq
                if impacts_caching:
                    cond_kwargs['use_caching'] = False
                return self_copy
        return reself

    @property
    def stats_defaults(self) -> tp.Kwargs:
        """Defaults for `ReturnsAccessor.stats`.

        Merges `vectorbt.generic.stats_builder.StatsBuilderMixin.stats_defaults`,
        defaults from `ReturnsAccessor.defaults` (acting as `settings`), and
        `returns.stats` in `vectorbt._settings.settings`"""
        from vectorbt._settings import settings
        returns_stats_cfg = settings['returns']['stats']

        return merge_dicts(
            StatsBuilderMixin.stats_defaults.__get__(self),
            dict(settings=self.defaults),
            dict(settings=dict(year_freq=self.year_freq)),
            returns_stats_cfg
        )

    metrics: tp.ClassVar[Config] = Config(
        dict(
            start=dict(
                title='Start',
                calc_func=lambda self: self.wrapper.index[0],
                agg_func=None
            ),
            end=dict(
                title='End',
                calc_func=lambda self: self.wrapper.index[-1],
                agg_func=None
            ),
            period=dict(
                title='Period',
                calc_func=lambda self:
                len(self.wrapper.index) * (self.wrapper.freq if self.wrapper.freq is not None else 1),
                agg_func=None
            ),
            total_return=dict(
                title='Total Return [%]',
                calc_func='total',
                post_calc_func=lambda self, out, kwargs: out * 100
            ),
            benchmark_return=dict(
                title='Benchmark Return [%]',
                calc_func=lambda benchmark_rets: benchmark_rets.vbt.returns.total() * 100,
                check_benchmark_rets=True
            ),
            ann_return=dict(
                title='Annualized Return [%]',
                calc_func='annualized',
                post_calc_func=lambda self, out, kwargs: out * 100,
                check_has_freq=True,
                check_has_year_freq=True
            ),
            ann_volatility=dict(
                title='Annualized Volatility [%]',
                calc_func='annualized_volatility',
                post_calc_func=lambda self, out, kwargs: out * 100,
                check_has_freq=True,
                check_has_year_freq=True
            ),
            max_dd=dict(
                title='Max Drawdown [%]',
                calc_func='drawdowns.max_drawdown',
                post_calc_func=lambda self, out, kwargs: -out * 100
            ),
            max_dd_duration=dict(
                title='Max Drawdown Duration',
                calc_func='drawdowns.max_duration'
            ),
            sharpe_ratio=dict(
                title='Sharpe Ratio',
                calc_func='sharpe_ratio',
                check_has_freq=True,
                check_has_year_freq=True
            ),
            calmar_ratio=dict(
                title='Calmar Ratio',
                calc_func='calmar_ratio',
                check_has_freq=True,
                check_has_year_freq=True
            ),
            omega_ratio=dict(
                title='Omega Ratio',
                calc_func='omega_ratio',
                check_has_freq=True,
                check_has_year_freq=True
            ),
            sortino_ratio=dict(
                title='Sortino Ratio',
                calc_func='sortino_ratio',
                check_has_freq=True,
                check_has_year_freq=True
            ),
            skew=dict(
                title='Skew',
                calc_func='obj.skew'
            ),
            kurtosis=dict(
                title='Kurtosis',
                calc_func='obj.kurtosis'
            ),
            tail_ratio=dict(
                title='Tail Ratio',
                calc_func='tail_ratio'
            ),
            common_sense_ratio=dict(
                title='Common Sense Ratio',
                calc_func='common_sense_ratio',
                check_has_freq=True,
                check_has_year_freq=True
            ),
            value_at_risk=dict(
                title='Value at Risk',
                calc_func='value_at_risk'
            ),
            alpha=dict(
                title='Alpha',
                calc_func='alpha',
                check_has_freq=True,
                check_has_year_freq=True,
                check_benchmark_rets=True
            ),
            beta=dict(
                title='Beta',
                calc_func='beta',
                check_benchmark_rets=True
            )
        ),
        copy_kwargs=dict(copy_mode='deep')
    )
    """Metrics supported by `ReturnsAccessor.stats`.

    !!! note
        It's safe to change this config - it's a (deep) copy of the class variable.
        
        But copying `ReturnsAccessor` using `ReturnsAccessor.copy` won't create a copy of the config."""


@register_series_accessor('returns')
class ReturnsSRAccessor(ReturnsAccessor, GenericSRAccessor):
    """Accessor on top of return series. For Series only.

    Accessible through `pd.Series.vbt.returns`."""

    def __init__(self, obj: tp.Series, year_freq: tp.Optional[tp.FrequencyLike] = None, **kwargs) -> None:
        if not checks.is_pandas(obj):  # parent accessor
            obj = obj._obj

        GenericSRAccessor.__init__(self, obj, **kwargs)
        ReturnsAccessor.__init__(self, obj, year_freq=year_freq, **kwargs)

    def plot_cumulative(self,
                        benchmark_rets: tp.Optional[tp.ArrayLike] = None,
                        start_value: float = 1,
                        fill_to_benchmark: bool = False,
                        main_kwargs: tp.KwargsLike = None,
                        benchmark_kwargs: tp.KwargsLike = None,
                        hline_shape_kwargs: tp.KwargsLike = None,
                        add_trace_kwargs: tp.KwargsLike = None,
                        xref: str = 'x',
                        yref: str = 'y',
                        fig: tp.Optional[tp.BaseFigure] = None,
                        **layout_kwargs) -> tp.BaseFigure:  # pragma: no cover
        """Plot cumulative returns.

        Args:
            benchmark_rets (array_like): Benchmark return to compare returns against.
                Will broadcast per element.
            start_value (float): The starting returns.
            fill_to_benchmark (bool): Whether to fill between main and benchmark, or between main and `start_value`.
            main_kwargs (dict): Keyword arguments passed to `vectorbt.generic.accessors.GenericSRAccessor.plot` for main.
            benchmark_kwargs (dict): Keyword arguments passed to `vectorbt.generic.accessors.GenericSRAccessor.plot` for benchmark.
            hline_shape_kwargs (dict): Keyword arguments passed to `plotly.graph_objects.Figure.add_shape` for `start_value` line.
            add_trace_kwargs (dict): Keyword arguments passed to `add_trace`.
            xref (str): X coordinate axis.
            yref (str): Y coordinate axis.
            fig (Figure or FigureWidget): Figure to add traces to.
            **layout_kwargs: Keyword arguments for layout.

        ## Example

        ```python-repl
        >>> import pandas as pd
        >>> import numpy as np

        >>> np.random.seed(0)
        >>> rets = pd.Series(np.random.uniform(-0.05, 0.05, size=100))
        >>> benchmark_rets = pd.Series(np.random.uniform(-0.05, 0.05, size=100))
        >>> rets.vbt.returns.plot_cumulative(benchmark_rets=benchmark_rets)
        ```

        ![](/vectorbt/docs/img/plot_cumulative.svg)
        """
        from vectorbt._settings import settings
        plotting_cfg = settings['plotting']

        if fig is None:
            fig = make_figure()
        fig.update_layout(**layout_kwargs)
        x_domain = get_domain(xref, fig)
        fill_to_benchmark = fill_to_benchmark and benchmark_rets is not None

        if benchmark_rets is not None:
            # Plot benchmark
            benchmark_rets = broadcast_to(benchmark_rets, self.obj)
            if benchmark_kwargs is None:
                benchmark_kwargs = {}
            benchmark_kwargs = merge_dicts(dict(
                trace_kwargs=dict(
                    line=dict(
                        color=plotting_cfg['color_schema']['gray']
                    ),
                    name='Benchmark'
                )
            ), benchmark_kwargs)
            benchmark_cumrets = benchmark_rets.vbt.returns.cumulative(start_value=start_value)
            benchmark_cumrets.vbt.plot(**benchmark_kwargs, add_trace_kwargs=add_trace_kwargs, fig=fig)
        else:
            benchmark_cumrets = None

        # Plot main
        if main_kwargs is None:
            main_kwargs = {}
        main_kwargs = merge_dicts(dict(
            trace_kwargs=dict(
                line=dict(
                    color=plotting_cfg['color_schema']['purple']
                )
            ),
            other_trace_kwargs='hidden'
        ), main_kwargs)
        cumrets = self.cumulative(start_value=start_value)
        if fill_to_benchmark:
            cumrets.vbt.plot_against(benchmark_cumrets, **main_kwargs, add_trace_kwargs=add_trace_kwargs, fig=fig)
        else:
            cumrets.vbt.plot_against(start_value, **main_kwargs, add_trace_kwargs=add_trace_kwargs, fig=fig)

        # Plot hline
        if hline_shape_kwargs is None:
            hline_shape_kwargs = {}
        fig.add_shape(**merge_dicts(dict(
            type='line',
            xref="paper",
            yref=yref,
            x0=x_domain[0],
            y0=start_value,
            x1=x_domain[1],
            y1=start_value,
            line=dict(
                color="gray",
                dash="dash",
            )
        ), hline_shape_kwargs))

        return fig


@register_dataframe_accessor('returns')
class ReturnsDFAccessor(ReturnsAccessor, GenericDFAccessor):
    """Accessor on top of return series. For DataFrames only.

    Accessible through `pd.DataFrame.vbt.returns`."""

    def __init__(self, obj: tp.Frame, year_freq: tp.Optional[tp.FrequencyLike] = None, **kwargs) -> None:
        if not checks.is_pandas(obj):  # parent accessor
            obj = obj._obj

        GenericDFAccessor.__init__(self, obj, **kwargs)
        ReturnsAccessor.__init__(self, obj, year_freq=year_freq, **kwargs)
