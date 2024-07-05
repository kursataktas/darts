import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal

import numpy as np
import pandas as pd

import darts.metrics
from darts import TimeSeries
from darts.logging import get_logger, raise_log
from darts.models.forecasting.forecasting_model import GlobalForecastingModel
from darts.utils import _with_sanity_checks
from darts.utils.historical_forecasts.utils import _historical_forecasts_start_warnings
from darts.utils.timeseries_generation import _build_forecast_series
from darts.utils.ts_utils import (
    SeriesType,
    get_series_seq_type,
    get_single_series,
    series2seq,
)
from darts.utils.utils import generate_index, n_steps_between

logger = get_logger(__name__)


def _triul_indices(forecast_horizon, n_comps):
    idx_horizon, idx_hfc = np.tril_indices(n=forecast_horizon, k=-1)
    idx_comp = [i for _ in range(len(idx_horizon)) for i in range(n_comps)]

    # reverse to get lower left triangle
    idx_horizon = forecast_horizon - 1 - idx_horizon
    idx_horizon = idx_horizon.repeat(n_comps)

    idx_hfc = idx_hfc.repeat(n_comps)
    return idx_horizon, idx_comp, idx_hfc


def cqr_score_sym(row, quantile_lo_col, quantile_hi_col):
    return (
        [None, None]
        if row[quantile_lo_col] is None or row[quantile_hi_col] is None
        else [
            max(row[quantile_lo_col] - row["y"], row["y"] - row[quantile_hi_col]),
            0
            if row[quantile_lo_col] - row["y"] > row["y"] - row[quantile_hi_col]
            else 1,
        ]
    )


def cqr_score_asym(row, quantile_lo_col, quantile_hi_col):
    return (
        [None, None]
        if row[quantile_lo_col] is None or row[quantile_hi_col] is None
        else [
            row[quantile_lo_col] - row["y"],
            row["y"] - row[quantile_hi_col],
            0
            if row[quantile_lo_col] - row["y"] > row["y"] - row[quantile_hi_col]
            else 1,
        ]
    )


# TODO: fit conformal model (maybe for the future)
# -


class ConformalModel(GlobalForecastingModel):
    def __init__(
        self,
        model,
        alpha: Union[float, Tuple[float, float]],
        method: str,
        quantiles: Optional[List[float]] = None,
    ):
        """Conformal prediction dataclass

        Parameters
        ----------
        model
            The forecasting model.
        alpha
            Significance level of the prediction interval, float if coverage error spread arbitrarily over left and
            right tails, tuple of two floats for different coverage error over left and right tails respectively
        method
            The conformal prediction technique to use:

             - `"naive"` for the Naive or Absolute Residual method
             - `"cqr"` for Conformalized Quantile Regression
        quantiles
            Optionally, a list of quantiles from the quantile regression `model` to use.
        """
        if not isinstance(model, GlobalForecastingModel) or not model._fit_called:
            raise_log(
                ValueError("`model` must be a pre-trained `GlobalForecastingModel`."),
                logger=logger,
            )
        if method == "naive" and not isinstance(alpha, float):
            raise_log(
                ValueError(f"`alpha` must be a `float` when `method={method}`."),
                logger=logger,
            )
        super().__init__(add_encoders=None)

        if isinstance(alpha, float):
            self.symmetrical = True
            self.q_hats = pd.DataFrame(columns=["q_hat_sym"])
        else:
            self.symmetrical = False
            self.alpha_lo, self.alpha_hi = alpha
            self.q_hats = pd.DataFrame(columns=["q_hat_lo", "q_hat_hi"])

        self.model = model
        self.noncon_scores = dict()
        self.alpha = alpha
        self.method = method
        self.quantiles = quantiles
        self._fit_called = True
        self.score_fn = darts.metrics.ae

    @property
    def output_chunk_length(self) -> Optional[int]:
        return self.model.output_chunk_length

    def fit(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
    ) -> "ConformalModel":
        # does not have to be trained
        return self

    def predict(
        self,
        n: int,
        series: Union[TimeSeries, Sequence[TimeSeries]] = None,
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        num_samples: int = 1,
        verbose: bool = False,
        predict_likelihood_parameters: bool = False,
        show_warnings: bool = True,
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:
        called_with_single_series = get_series_seq_type(series) == SeriesType.SINGLE
        series = series2seq(series)
        past_covariates = series2seq(past_covariates)
        future_covariates = series2seq(future_covariates)

        preds = self.model.predict(
            n=n,
            series=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            num_samples=num_samples,
            verbose=verbose,
            predict_likelihood_parameters=predict_likelihood_parameters,
            show_warnings=show_warnings,
        )
        residuals = self.model.residuals(
            series=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            forecast_horizon=n,
            last_points_only=False,
            retrain=False,
            stride=1,
            verbose=verbose,
            show_warnings=show_warnings,
            values_only=True,
            metric=self.score_fn,
        )
        if self.method != "naive":
            raise_log(NotImplementedError("non-naive not yet implemented"))

        # first: NAIVE only
        cp_preds = []
        for series_, pred, res in zip(series, preds, residuals):
            # convert to (horizon, n comps, hist fcs)
            res = np.concatenate(res, axis=2)
            q_hat = np.quantile(res, q=self.alpha, axis=2)
            pred_vals = pred.values(copy=False)
            cp_pred = np.concatenate(
                [pred_vals - q_hat, pred_vals, pred_vals + q_hat], axis=1
            )
            cp_pred = _build_forecast_series(
                points_preds=cp_pred,
                input_series=series_,
                custom_columns=self._cp_component_names(series_),
                time_index=pred._time_index,
                with_static_covs=False,
                with_hierarchy=False,
            )
            cp_preds.append(cp_pred)
        return cp_preds[0] if called_with_single_series else cp_preds
        # for step_number in range(1, self.n_forecasts + 1):
        #     # conformalize
        #     noncon_scores = self._get_nonconformity_scores(df_cal, step_number)
        #     q_hat = self._get_q_hat(df_cal, noncon_scores)
        #     y_hat_col = f"yhat{step_number}"
        #     y_hat_lo_col = f"{y_hat_col} {min(self.quantiles) * 100}%"
        #     y_hat_hi_col = f"{y_hat_col} {max(self.quantiles) * 100}%"
        #     if self.method == "naive" and self.symmetrical:
        #         q_hat_sym = q_hat["q_hat_sym"]
        #         df[y_hat_lo_col] = df[y_hat_col] - q_hat_sym
        #         df[y_hat_hi_col] = df[y_hat_col] + q_hat_sym
        #     elif self.method == "cqr" and self.symmetrical:
        #         q_hat_sym = q_hat["q_hat_sym"]
        #         df[y_hat_lo_col] = df[y_hat_lo_col] - q_hat_sym
        #         df[y_hat_hi_col] = df[y_hat_hi_col] + q_hat_sym
        #     elif self.method == "cqr" and not self.symmetrical:
        #         q_hat_lo = q_hat["q_hat_lo"]
        #         q_hat_hi = q_hat["q_hat_hi"]
        #         df[y_hat_lo_col] = df[y_hat_lo_col] - q_hat_lo
        #         df[y_hat_hi_col] = df[y_hat_hi_col] + q_hat_hi
        #     else:
        #         raise ValueError(
        #             f"Unknown conformal prediction method '{self.method}'. Please input either 'naive' or 'cqr'."
        #         )
        #     if step_number == 1:
        #         # save nonconformity scores of the first timestep
        #         self.noncon_scores = noncon_scores
        #
        #     # append the dictionary of q_hats to the dataframe based on the keys of the dictionary
        #     q_hat_df = pd.DataFrame([q_hat])
        #     self.q_hats = pd.concat([self.q_hats, q_hat_df], ignore_index=True)
        #
        #     # if show_all_PI is True, add the quantile regression prediction intervals
        #     if show_all_PI:
        #         df_quantiles = [col for col in df_qr.columns if "%" in col and f"yhat{step_number}" in col]
        #         df_add = df_qr[df_quantiles]
        #
        #         if self.method == "naive":
        #             cp_lo_col = f"yhat{step_number} - qhat{step_number}"  # e.g. yhat1 - qhat1
        #             cp_hi_col = f"yhat{step_number} + qhat{step_number}"  # e.g. yhat1 + qhat1
        #             df.rename(columns={y_hat_lo_col: cp_lo_col, y_hat_hi_col: cp_hi_col}, inplace=True)
        #         elif self.method == "cqr":
        #             qr_lo_col = (
        #                 f"yhat{step_number} {max(self.quantiles) * 100}% - qhat{step_number}"  #e.g. yhat1 95% - qhat1
        #             )
        #             qr_hi_col = (
        #                 f"yhat{step_number} {min(self.quantiles) * 100}% + qhat{step_number}"  #e.g. yhat1 5% + qhat1
        #             )
        #             df.rename(columns={y_hat_lo_col: qr_lo_col, y_hat_hi_col: qr_hi_col}, inplace=True)
        #
        #         df = pd.concat([df, df_add], axis=1, ignore_index=False)
        #
        # return df

    @_with_sanity_checks("_historical_forecasts_sanity_checks")
    def historical_forecasts(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        future_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
        num_samples: int = 1,
        train_length: Optional[int] = None,
        start: Optional[Union[pd.Timestamp, float, int]] = None,
        start_format: Literal["position", "value"] = "value",
        forecast_horizon: int = 1,
        stride: int = 1,
        retrain: Union[bool, int, Callable[..., bool]] = True,
        overlap_end: bool = False,
        last_points_only: bool = True,
        verbose: bool = False,
        show_warnings: bool = True,
        predict_likelihood_parameters: bool = False,
        enable_optimization: bool = True,
        fit_kwargs: Optional[Dict[str, Any]] = None,
        predict_kwargs: Optional[Dict[str, Any]] = None,
        sample_weight: Optional[Union[TimeSeries, Sequence[TimeSeries], str]] = None,
    ) -> Union[TimeSeries, List[TimeSeries], List[List[TimeSeries]]]:
        called_with_single_series = get_series_seq_type(series) == SeriesType.SINGLE
        series = series2seq(series)
        past_covariates = series2seq(past_covariates)
        future_covariates = series2seq(future_covariates)

        # generate all possible forecasts (overlap_end=True)
        hfcs = self.model.historical_forecasts(
            series=series,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            num_samples=num_samples,
            forecast_horizon=forecast_horizon,
            retrain=False,
            overlap_end=True,
            last_points_only=last_points_only,
            verbose=verbose,
            show_warnings=show_warnings,
            predict_likelihood_parameters=predict_likelihood_parameters,
            enable_optimization=enable_optimization,
            fit_kwargs=fit_kwargs,
            predict_kwargs=predict_kwargs,
        )
        # TODO: add support for:
        # - num_samples
        # - predict_likelihood_parameters
        # - tqdm iterator over series
        # - support for different CP algorithms

        # DONE:
        # - properly define minimum residuals to start (different for `last_points_only=True/False`
        # - compute all possible residuals (including the partial forecast horizons up until the end)
        # - overlap_end = True
        # - last_points_only = True
        # - add correct output components
        # - use only `train_length` previous residuals
        residuals = self.model.residuals(
            series=series,
            historical_forecasts=hfcs,
            overlap_end=True,
            last_points_only=last_points_only,
            verbose=verbose,
            show_warnings=show_warnings,
            values_only=True,
            metric=self.score_fn,
        )

        # this mask is later used to avoid look-ahead bias in case of `last_points_only=False`
        idx_horizon, idx_comp, idx_hfc = _triul_indices(
            forecast_horizon, series[0].width
        )

        cp_hfcs = []
        for series_idx, (series_, s_hfcs, res) in enumerate(
            zip(series, hfcs, residuals)
        ):
            cp_preds = []

            # no historical forecasts were generated
            if not s_hfcs or (train_length is not None and train_length > len(s_hfcs)):
                cp_hfcs.append(cp_preds)
                continue

            # determine the last forecast index for conformal prediction
            first_hfc = get_single_series(s_hfcs)
            last_hfc = s_hfcs if last_points_only else s_hfcs[-1]
            last_fc_idx = len(s_hfcs)
            # adjust based on `overlap_end`
            if not overlap_end:
                delta_end = n_steps_between(
                    end=last_hfc.end_time(),
                    start=series_.end_time(),
                    freq=series_.freq,
                )
                if last_fc_idx:
                    last_fc_idx -= delta_end

            # determine the first forecast index for conformal prediction; all forecasts before that are
            # used for calibration
            # we need at least 1 residual per point in the horizon
            skip_n_train = forecast_horizon

            # plus some additional steps based on `train_length`
            if train_length is not None:
                skip_n_train += train_length - 1

            # skip solely based on `start`
            skip_n_start = 0
            if start is not None:
                if isinstance(start, pd.Timestamp) or start_format == "value":
                    start_time = start
                else:
                    start_time = series_._time_index[start]

                skip_n_start = n_steps_between(
                    end=start_time,
                    start=first_hfc.start_time(),
                    freq=series_.freq,
                )
                # hfcs only contain last predicted points; skip until end of first forecast
                if last_points_only:
                    skip_n_start += forecast_horizon - 1

                # if start is out of bounds, we ignore it
                if (
                    skip_n_start < 0
                    or skip_n_start >= last_fc_idx
                    or skip_n_start < skip_n_train
                ):
                    skip_n_start = 0
                    if show_warnings:
                        # adjust to actual start point in case of `last_points_only`
                        adjust_idx = (
                            int(last_points_only)
                            * (forecast_horizon - 1)
                            * series_.freq
                        )
                        hfc_predict_index = (
                            s_hfcs[skip_n_train].start_time() - adjust_idx,
                            s_hfcs[last_fc_idx].start_time() - adjust_idx,
                        )
                        _historical_forecasts_start_warnings(
                            idx=series_idx,
                            start=start,
                            start_time_=start_time,
                            historical_forecasts_time_index=hfc_predict_index,
                        )

            # get final first index
            first_fc_idx = max([skip_n_train, skip_n_start])
            if first_fc_idx >= last_fc_idx:
                (
                    raise_log(
                        ValueError(
                            "Cannot build a single input for prediction with the provided model, "
                            f"`series` and `*_covariates` at series index: {series_idx}. The minimum "
                            "prediction input time index requirements were not met. "
                            "Please check the time index of `series` and `*_covariates`."
                        ),
                        logger=logger,
                    ),
                )

            # historical conformal prediction
            if last_points_only:
                for idx, pred_vals in enumerate(
                    s_hfcs.values(copy=False)[first_fc_idx:last_fc_idx:stride]
                ):
                    pred_vals = np.expand_dims(pred_vals, 0)
                    if not first_fc_idx and not idx:
                        cp_pred = np.concatenate([pred_vals] * 3, axis=1)
                    else:
                        # get the last residual index for calibration, `cal_end` is exclusive
                        # to avoid look-ahead bias, use only residuals from before the historical forecast start point;
                        # since we look at `last_points only=True`, the last residual historically available at
                        # the forecasting point is `forecast_horizon - 1` steps before
                        cal_end = first_fc_idx + idx * stride - (forecast_horizon - 1)
                        # first residual index is shifted back by the horizon to get `train_length` points for
                        # the last point in the horizon
                        cal_start = (
                            cal_end - train_length if train_length is not None else None
                        )
                        cal_res = res[cal_start:cal_end]
                        q_hat = np.nanquantile(cal_res, q=self.alpha, axis=0)
                        cp_pred = np.concatenate(
                            [pred_vals - q_hat, pred_vals, pred_vals + q_hat], axis=1
                        )
                    cp_preds.append(cp_pred)
                cp_preds = _build_forecast_series(
                    points_preds=np.concatenate(cp_preds, axis=0),
                    input_series=series_,
                    custom_columns=self._cp_component_names(series_),
                    time_index=generate_index(
                        start=s_hfcs._time_index[first_fc_idx],
                        length=len(cp_preds),
                        freq=series_.freq * stride,
                    ),
                    with_static_covs=False,
                    with_hierarchy=False,
                )
                cp_hfcs.append(cp_preds)
            else:
                for idx, pred in enumerate(s_hfcs[first_fc_idx:last_fc_idx:stride]):
                    # convert to (horizon, n comps, hist fcs)
                    pred_vals = pred.values(copy=False)
                    if not first_fc_idx and not idx:
                        cp_pred = np.concatenate([pred_vals] * 3, axis=1)
                    else:
                        # get the last residual index for calibration, `cal_end` is exclusive
                        # to avoid look-ahead bias, use only residuals from before the historical forecast start point;
                        # since we look at `last_points only=False`, the last residual historically available at
                        # the forecasting point is from the first predicted step of the previous forecast
                        cal_end = first_fc_idx + idx * stride
                        # stepping back further gives access to more residuals and also residuals from longer horizons.
                        # to get `train_length` residuals for the last step in the horizon, we need to step back
                        # additional `forecast_horizon - 1` points
                        cal_start = (
                            cal_end - train_length - (forecast_horizon - 1)
                            if train_length is not None
                            else None
                        )
                        # TODO: should we consider all previous historical forecasts, or only the stridden ones?
                        cal_res = np.concatenate(res[cal_start:cal_end], axis=2)
                        # ignore upper left residuals to have same number of residuals per horizon
                        cal_res[idx_horizon, idx_comp, idx_hfc] = np.nan
                        # ignore lower right residuals to avoid look-ahead bias
                        cal_res[
                            forecast_horizon - 1 - idx_horizon,
                            idx_comp,
                            cal_res.shape[2] - 1 - idx_hfc,
                        ] = np.nan
                        q_hat = np.nanquantile(cal_res, q=self.alpha, axis=2)
                        cp_pred = np.concatenate(
                            [pred_vals - q_hat, pred_vals, pred_vals + q_hat], axis=1
                        )
                    cp_pred = _build_forecast_series(
                        points_preds=cp_pred,
                        input_series=series_,
                        custom_columns=self._cp_component_names(series_),
                        time_index=pred._time_index,
                        with_static_covs=False,
                        with_hierarchy=False,
                    )
                    cp_preds.append(cp_pred)
                cp_hfcs.append(cp_preds)
        return cp_hfcs[0] if called_with_single_series else cp_hfcs

    def _get_nonconformity_scores(self, df_cal: pd.DataFrame, step_number: int) -> dict:
        """Get the nonconformity scores using the given conformal prediction technique.

        Parameters
        ----------
            df_cal : pd.DataFrame
                calibration dataframe
            step_number : int
                i-th step ahead forecast

            Returns
            -------
                Dict[str, np.ndarray]
                    dictionary with one entry (symmetrical) or two entries (asymmetrical) of nonconformity scores

        """
        y_hat_col = f"yhat{step_number}"
        if self.method == "cqr":
            # CQR nonconformity scoring function
            quantile_lo = str(min(self.quantiles) * 100)
            quantile_hi = str(max(self.quantiles) * 100)
            quantile_lo_col = f"{y_hat_col} {quantile_lo}%"
            quantile_hi_col = f"{y_hat_col} {quantile_hi}%"
            if self.symmetrical:
                scores_df = df_cal.apply(
                    cqr_score_sym,
                    axis=1,
                    result_type="expand",
                    quantile_lo_col=quantile_lo_col,
                    quantile_hi_col=quantile_hi_col,
                )
                scores_df.columns = ["scores", "arg"]
                noncon_scores = scores_df["scores"].values
            else:  # asymmetrical intervals
                scores_df = df_cal.apply(
                    cqr_score_asym,
                    axis=1,
                    result_type="expand",
                    quantile_lo_col=quantile_lo_col,
                    quantile_hi_col=quantile_hi_col,
                )
                scores_df.columns = ["scores_lo", "scores_hi", "arg"]
                noncon_scores_lo = scores_df["scores_lo"].values
                noncon_scores_hi = scores_df["scores_hi"].values
                # Remove NaN values
                noncon_scores_lo: Any = noncon_scores_lo[~pd.isnull(noncon_scores_lo)]
                noncon_scores_hi: Any = noncon_scores_hi[~pd.isnull(noncon_scores_hi)]
                # Sort
                noncon_scores_lo.sort()
                noncon_scores_hi.sort()
                # return dict of nonconformity scores
                return {
                    "noncon_scores_hi": noncon_scores_lo,
                    "noncon_scores_lo": noncon_scores_hi,
                }
        else:  # self.method == "naive"
            # Naive nonconformity scoring function
            noncon_scores = abs(df_cal["y"] - df_cal[y_hat_col]).values
        # Remove NaN values
        noncon_scores: Any = noncon_scores[~pd.isnull(noncon_scores)]
        # Sort
        noncon_scores.sort()

        return {"noncon_scores": noncon_scores}

    def _get_q_hat(self, noncon_scores: dict) -> dict:
        """Get the q_hat that is derived from the nonconformity scores.

        Parameters
        ----------
            noncon_scores : dict
                dictionary with one entry (symmetrical) or two entries (asymmetrical) of nonconformity scores

            Returns
            -------
                Dict[str, float]
                    upper and lower q_hat value, or the one-sided prediction interval width

        """
        # Get the q-hat index and value
        if self.method == "cqr" and self.symmetrical is False:
            noncon_scores_lo = noncon_scores["noncon_scores_lo"]
            noncon_scores_hi = noncon_scores["noncon_scores_hi"]
            q_hat_idx_lo = int(len(noncon_scores_lo) * self.alpha_lo)
            q_hat_idx_hi = int(len(noncon_scores_hi) * self.alpha_hi)
            q_hat_lo = noncon_scores_lo[-q_hat_idx_lo]
            q_hat_hi = noncon_scores_hi[-q_hat_idx_hi]
            return {"q_hat_lo": q_hat_lo, "q_hat_hi": q_hat_hi}
        else:
            noncon_scores = noncon_scores["noncon_scores"]
            q_hat_idx = int(len(noncon_scores) * self.alpha)
            q_hat = noncon_scores[-q_hat_idx]
            return {"q_hat_sym": q_hat}

    def _cp_component_names(self, input_series) -> List[str]:
        return [
            f"{tgt_name}_{param_n}"
            for tgt_name in input_series.components
            for param_n in ["q_lo", "q_md", "q_hi"]
        ]

    @property
    def _model_encoder_settings(
        self,
    ) -> Tuple[
        Optional[int],
        Optional[int],
        bool,
        bool,
        Optional[List[int]],
        Optional[List[int]],
    ]:
        return None, None, False, False, None, None

    @property
    def extreme_lags(
        self,
    ) -> Tuple[
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
        int,
        Optional[int],
    ]:
        return self.model.extreme_lags

    def supports_multivariate(self) -> bool:
        return self.model.supports_multivariate


def uncertainty_evaluate(df_forecast: pd.DataFrame) -> pd.DataFrame:
    """Evaluate conformal prediction on test dataframe.

    Parameters
    ----------
        df_forecast : pd.DataFrame
            forecast dataframe with the conformal prediction intervals

    Returns
    -------
        pd.DataFrame
            table containing evaluation metrics such as interval_width and miscoverage_rate
    """
    # Remove beginning rows used as lagged regressors (if any), or future dataframes without y-values
    # therefore, this ensures that all forecast rows for evaluation contains both y and y-hat
    df_forecast_eval = df_forecast.dropna(subset=["y", "yhat1"]).reset_index(drop=True)

    # Get evaluation params
    df_eval = pd.DataFrame()
    cols = df_forecast_eval.columns
    yhat_cols = [col for col in cols if "%" in col]
    n_forecasts = int(re.search("yhat(\\d+)", yhat_cols[-1]).group(1))

    # get the highest and lowest quantile percentages
    quantiles = []
    for col in yhat_cols:
        match = re.search(r"\d+\.\d+", col)
        if match:
            quantiles.append(float(match.group()))
    quantiles = sorted(set(quantiles))

    # Begin conformal evaluation steps
    for step_number in range(1, n_forecasts + 1):
        y = df_forecast_eval["y"].values
        # only relevant if show_all_PI is true
        if len([col for col in cols if "qhat" in col]) > 0:
            qhat_cols = [col for col in cols if f"qhat{step_number}" in col]
            yhat_lo = df_forecast_eval[qhat_cols[0]].values
            yhat_hi = df_forecast_eval[qhat_cols[-1]].values
        else:
            yhat_lo = df_forecast_eval[f"yhat{step_number} {quantiles[0]}%"].values
            yhat_hi = df_forecast_eval[f"yhat{step_number} {quantiles[-1]}%"].values
        interval_width, miscoverage_rate = _get_evaluate_metrics_from_dataset(
            y, yhat_lo, yhat_hi
        )

        # Construct row dataframe with current timestep using its q-hat, interval width, and miscoverage rate
        col_names = ["interval_width", "miscoverage_rate"]
        row = [interval_width, miscoverage_rate]
        df_row = pd.DataFrame(
            [row],
            columns=pd.MultiIndex.from_product([[f"yhat{step_number}"], col_names]),
        )

        # Add row dataframe to overall evaluation dataframe with all forecasted timesteps
        df_eval = pd.concat([df_eval, df_row], axis=1)

    return df_eval


def _get_evaluate_metrics_from_dataset(
    y: np.ndarray, yhat_lo: np.ndarray, yhat_hi: np.ndarray
) -> Tuple[float, float]:
    #     df_forecast_eval: pd.DataFrame,
    #     quantile_lo_col: str,
    #     quantile_hi_col: str,
    # ) -> Tuple[float, float]:
    """Infers evaluation parameters based on the evaluation dataframe columns.

    Parameters
    ----------
        df_forecast_eval : pd.DataFrame
            forecast dataframe with the conformal prediction intervals

    Returns
    -------
        float, float
            conformal prediction evaluation metrics
    """
    # Interval width (efficiency metric)
    quantile_lo_mean = np.mean(yhat_lo)
    quantile_hi_mean = np.mean(yhat_hi)
    interval_width = quantile_hi_mean - quantile_lo_mean

    # Miscoverage rate (validity metric)
    n_covered = np.sum((y >= yhat_lo) & (y <= yhat_hi))
    coverage_rate = n_covered / len(y)
    miscoverage_rate = 1 - coverage_rate

    return interval_width, miscoverage_rate