import copy
import itertools
import os

import numpy as np
import pandas as pd
import pytest

from darts import TimeSeries, concatenate
from darts.datasets import AirPassengersDataset
from darts.metrics import ae, err, ic, incs_qr, mic
from darts.models import (
    ConformalNaiveModel,
    ConformalQRModel,
    LinearRegressionModel,
    NaiveSeasonal,
    NLinearModel,
)
from darts.models.forecasting.forecasting_model import ForecastingModel
from darts.tests.conftest import TORCH_AVAILABLE, tfm_kwargs
from darts.utils import timeseries_generation as tg
from darts.utils.utils import (
    likelihood_component_names,
    quantile_interval_names,
    quantile_names,
)

IN_LEN = 3
OUT_LEN = 3
regr_kwargs = {"lags": IN_LEN, "output_chunk_length": OUT_LEN}
tfm_kwargs = copy.deepcopy(tfm_kwargs)
tfm_kwargs["pl_trainer_kwargs"]["fast_dev_run"] = True
torch_kwargs = dict(
    {"input_chunk_length": IN_LEN, "output_chunk_length": OUT_LEN, "random_state": 0},
    **tfm_kwargs,
)

q = [0.1, 0.5, 0.9]


def train_model(
    *args, model_type="regression", model_params=None, quantiles=None, **kwargs
):
    model_params = model_params or {}
    if model_type == "regression":
        return LinearRegressionModel(
            **regr_kwargs,
            **model_params,
            random_state=42,
        ).fit(*args, **kwargs)
    elif model_type in ["regression_prob", "regression_qr"]:
        return LinearRegressionModel(
            likelihood="quantile",
            quantiles=quantiles,
            **regr_kwargs,
            **model_params,
            random_state=42,
        ).fit(*args, **kwargs)
    else:
        return NLinearModel(**torch_kwargs, **model_params).fit(*args, **kwargs)


# pre-trained global model for conformal models
models_cls_kwargs_errs = [
    (
        ConformalNaiveModel,
        {"quantiles": q},
        "regression",
    ),
]

if TORCH_AVAILABLE:
    models_cls_kwargs_errs.append((
        ConformalNaiveModel,
        {"quantiles": q},
        "torch",
    ))


class TestConformalModel:
    """
    Tests all general model behavior for Naive Conformal Model with symmetric non-conformity score.
    Additionally, checks correctness of predictions for:
    - ConformalNaiveModel with symmetric & asymmetric non-conformity scores
    - ConformaQRlModel with symmetric & asymmetric non-conformity scores
    """

    np.random.seed(42)

    # forecasting horizon used in runnability tests
    horizon = OUT_LEN + 1

    # some arbitrary static covariates
    static_covariates = pd.DataFrame([[0.0, 1.0]], columns=["st1", "st2"])

    # real timeseries for functionality tests
    ts_length = 13 + horizon
    ts_passengers = (
        AirPassengersDataset()
        .load()[:ts_length]
        .with_static_covariates(static_covariates)
    )
    ts_pass_train, ts_pass_val = (
        ts_passengers[:-horizon],
        ts_passengers[-horizon:],
    )

    # an additional noisy series
    ts_pass_train_1 = ts_pass_train + 0.01 * tg.gaussian_timeseries(
        length=len(ts_pass_train),
        freq=ts_pass_train.freq_str,
        start=ts_pass_train.start_time(),
    )

    # an additional time series serving as covariates
    year_series = tg.datetime_attribute_timeseries(ts_passengers, attribute="year")
    month_series = tg.datetime_attribute_timeseries(ts_passengers, attribute="month")
    time_covariates = year_series.stack(month_series)
    time_covariates_train = time_covariates[:-horizon]

    # various ts with different static covariates representations
    ts_w_static_cov = tg.linear_timeseries(length=ts_length).with_static_covariates(
        pd.Series([1, 2])
    )
    ts_shared_static_cov = ts_w_static_cov.stack(tg.sine_timeseries(length=ts_length))
    ts_comps_static_cov = ts_shared_static_cov.with_static_covariates(
        pd.DataFrame([[0, 1], [2, 3]], columns=["st1", "st2"])
    )

    def test_model_construction_naive(self):
        local_model = NaiveSeasonal(K=5)
        global_model = LinearRegressionModel(**regr_kwargs)
        series = self.ts_pass_train

        model_err_msg = "`model` must be a pre-trained `GlobalForecastingModel`."
        # un-trained local model
        with pytest.raises(ValueError) as exc:
            ConformalNaiveModel(model=local_model, quantiles=q)
        assert str(exc.value) == model_err_msg

        # pre-trained local model
        local_model.fit(series)
        with pytest.raises(ValueError) as exc:
            ConformalNaiveModel(model=local_model, quantiles=q)
        assert str(exc.value) == model_err_msg

        # un-trained global model
        with pytest.raises(ValueError) as exc:
            ConformalNaiveModel(model=global_model, quantiles=q)
        assert str(exc.value) == model_err_msg

        # pre-trained local model should work
        global_model.fit(series)
        _ = ConformalNaiveModel(model=global_model, quantiles=q)

        # non-centered quantiles
        with pytest.raises(ValueError) as exc:
            ConformalNaiveModel(model=global_model, quantiles=[0.2, 0.5, 0.6])
        assert str(exc.value) == (
            "quantiles lower than `q=0.5` need to share same difference to `0.5` as quantiles higher than `q=0.5`"
        )

        # quantiles missing median
        with pytest.raises(ValueError) as exc:
            ConformalNaiveModel(model=global_model, quantiles=[0.1, 0.9])
        assert str(exc.value) == "median quantile `q=0.5` must be in `quantiles`"

        # too low and high quantiles
        with pytest.raises(ValueError) as exc:
            ConformalNaiveModel(model=global_model, quantiles=[-0.1, 0.5, 1.1])
        assert str(exc.value) == "All provided quantiles must be between 0 and 1."

    def test_model_construction_cqr(self):
        model_det = train_model(self.ts_pass_train, model_type="regression")
        model_prob_q = train_model(
            self.ts_pass_train, model_type="regression_prob", quantiles=q
        )
        model_prob_poisson = train_model(
            self.ts_pass_train,
            model_type="regression",
            model_params={"likelihood": "poisson"},
        )

        # deterministic global model
        with pytest.raises(ValueError) as exc:
            ConformalQRModel(model=model_det, quantiles=q)
        assert str(exc.value).startswith(
            "`model` must must support probabilistic forecasting."
        )
        # probabilistic model works
        _ = ConformalQRModel(model=model_prob_q, quantiles=q)
        # works also with different likelihood
        _ = ConformalQRModel(model=model_prob_poisson, quantiles=q)

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_save_model_parameters(self, config):
        # model creation parameters were saved before. check if re-created model has same params as original
        model_cls, kwargs, model_type = config
        model = model_cls(
            model=train_model(
                self.ts_pass_train, model_type=model_type, quantiles=kwargs["quantiles"]
            ),
            **kwargs,
        )
        model_fresh = model.untrained_model()
        assert model._model_params.keys() == model_fresh._model_params.keys()
        for param, val in model._model_params.items():
            if isinstance(val, ForecastingModel):
                # Conformal Models require a forecasting model as input, which has no equality
                continue
            assert val == model_fresh._model_params[param]

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_save_load_model(self, tmpdir_fn, config):
        # check if save and load methods work and if loaded model creates same forecasts as original model
        model_cls, kwargs, model_type = config
        model = model_cls(
            train_model(
                self.ts_pass_train, model_type=model_type, quantiles=kwargs["quantiles"]
            ),
            **kwargs,
        )
        model_prediction = model.predict(5)

        # check if save and load methods work and
        # if loaded conformal model creates same forecasts as original ensemble models
        cwd = os.getcwd()
        os.chdir(tmpdir_fn)
        expected_suffixes = [
            ".pkl",
            ".pkl.NLinearModel.pt",
            ".pkl.NLinearModel.pt.ckpt",
        ]

        # test save
        model.save()
        model.save(os.path.join(tmpdir_fn, f"{model_cls.__name__}.pkl"))

        assert os.path.exists(tmpdir_fn)
        files = os.listdir(tmpdir_fn)
        if model_type == "torch":
            # 1 from conformal model, 2 from torch, * 2 as `save()` was called twice
            assert len(files) == 6
            for f in files:
                assert f.startswith(model_cls.__name__)
            suffix_counts = {
                suffix: sum(1 for p in os.listdir(tmpdir_fn) if p.endswith(suffix))
                for suffix in expected_suffixes
            }
            assert all(count == 2 for count in suffix_counts.values())
        else:
            assert len(files) == 2
            for f in files:
                assert f.startswith(model_cls.__name__) and f.endswith(".pkl")

        # test load
        pkl_files = []
        for filename in os.listdir(tmpdir_fn):
            if filename.endswith(".pkl"):
                pkl_files.append(os.path.join(tmpdir_fn, filename))
        for p in pkl_files:
            loaded_model = model_cls.load(p)
            assert model_prediction == loaded_model.predict(5)
        os.chdir(cwd)

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_single_ts(self, config):
        model_cls, kwargs, model_type = config
        model = model_cls(
            train_model(
                self.ts_pass_train, model_type=model_type, quantiles=kwargs["quantiles"]
            ),
            **kwargs,
        )
        pred = model.predict(n=self.horizon)
        assert pred.n_components == self.ts_pass_train.n_components * 3
        assert not np.isnan(pred.all_values()).any().any()

        pred_fc = model.model.predict(n=self.horizon)
        assert pred_fc.time_index.equals(pred.time_index)
        # the center forecasts must be equal to the forecasting model forecast
        fc_columns = likelihood_component_names(
            self.ts_pass_val.columns, quantile_names([0.5])
        )
        np.testing.assert_array_almost_equal(
            pred[fc_columns].all_values(), pred_fc.all_values()
        )
        assert pred.static_covariates is None

        # using a different `n`, gives different results, since we can generate more residuals for the horizon
        pred1 = model.predict(n=1)
        assert not pred1 == pred

        # giving the same series as calibration set must give the same results
        pred_cal = model.predict(n=self.horizon, cal_series=self.ts_pass_train)
        np.testing.assert_array_almost_equal(pred.all_values(), pred_cal.all_values())

        # wrong dimension
        with pytest.raises(ValueError):
            model.predict(
                n=self.horizon, series=self.ts_pass_train.stack(self.ts_pass_train)
            )

    @pytest.mark.parametrize("config", models_cls_kwargs_errs[:])
    def test_multi_ts(self, config):
        model_cls, kwargs, model_type = config
        model = model_cls(
            train_model(
                [self.ts_pass_train, self.ts_pass_train_1],
                model_type=model_type,
                quantiles=kwargs["quantiles"],
            ),
            **kwargs,
        )
        with pytest.raises(ValueError):
            # when model is fit from >1 series, one must provide a series in argument
            model.predict(n=1)

        pred = model.predict(n=self.horizon, series=self.ts_pass_train)
        assert pred.n_components == self.ts_pass_train.n_components * 3
        assert not np.isnan(pred.all_values()).any().any()

        # the center forecasts must be equal to the forecasting model forecast
        fc_columns = likelihood_component_names(
            self.ts_pass_val.columns, quantile_names([0.5])
        )
        pred_fc = model.model.predict(n=self.horizon, series=self.ts_pass_train)
        assert pred_fc.time_index.equals(pred.time_index)
        np.testing.assert_array_almost_equal(
            pred[fc_columns].all_values(), pred_fc.all_values()
        )

        # using a calibration series also requires an input series
        with pytest.raises(ValueError):
            # when model is fit from >1 series, one must provide a series in argument
            model.predict(n=1, cal_series=self.ts_pass_train)
        # giving the same series as calibration set must give the same results
        pred_cal = model.predict(
            n=self.horizon,
            series=self.ts_pass_train,
            cal_series=self.ts_pass_train,
        )
        np.testing.assert_array_almost_equal(pred.all_values(), pred_cal.all_values())

        # check prediction for several time series
        pred_list = model.predict(
            n=self.horizon,
            series=[self.ts_pass_train, self.ts_pass_train_1],
        )
        pred_fc_list = model.model.predict(
            n=self.horizon,
            series=[self.ts_pass_train, self.ts_pass_train_1],
        )
        assert (
            len(pred_list) == 2
        ), f"Model {model_cls} did not return a list of prediction"
        for pred, pred_fc in zip(pred_list, pred_fc_list):
            assert pred.n_components == self.ts_pass_train.n_components * 3
            assert pred_fc.time_index.equals(pred.time_index)
            assert not np.isnan(pred.all_values()).any().any()
            np.testing.assert_array_almost_equal(
                pred_fc.all_values(),
                pred[fc_columns].all_values(),
            )

        # using a calibration series requires to have same number of series as target
        with pytest.raises(ValueError) as exc:
            # when model is fit from >1 series, one must provide a series in argument
            model.predict(
                n=1,
                series=[self.ts_pass_train, self.ts_pass_val],
                cal_series=self.ts_pass_train,
            )
        assert (
            str(exc.value)
            == "Mismatch between number of `cal_series` (1) and number of `series` (2)."
        )
        # using a calibration series requires to have same number of series as target
        with pytest.raises(ValueError) as exc:
            # when model is fit from >1 series, one must provide a series in argument
            model.predict(
                n=1,
                series=[self.ts_pass_train, self.ts_pass_val],
                cal_series=[self.ts_pass_train] * 3,
            )
        assert (
            str(exc.value)
            == "Mismatch between number of `cal_series` (3) and number of `series` (2)."
        )

        # giving the same series as calibration set must give the same results
        pred_cal_list = model.predict(
            n=self.horizon,
            series=[self.ts_pass_train, self.ts_pass_train_1],
            cal_series=[self.ts_pass_train, self.ts_pass_train_1],
        )
        for pred, pred_cal in zip(pred_list, pred_cal_list):
            np.testing.assert_array_almost_equal(
                pred.all_values(), pred_cal.all_values()
            )

        # using copies of the same series as calibration set must give the same interval widths for
        # each target series
        pred_cal_list = model.predict(
            n=self.horizon,
            series=[self.ts_pass_train, self.ts_pass_train_1],
            cal_series=[self.ts_pass_train, self.ts_pass_train],
        )

        pred_0_vals = pred_cal_list[0].all_values()
        pred_1_vals = pred_cal_list[1].all_values()

        # lower range
        np.testing.assert_array_almost_equal(
            pred_0_vals[:, 1] - pred_0_vals[:, 0], pred_1_vals[:, 1] - pred_1_vals[:, 0]
        )
        # upper range
        np.testing.assert_array_almost_equal(
            pred_0_vals[:, 2] - pred_0_vals[:, 1], pred_1_vals[:, 2] - pred_1_vals[:, 1]
        )

        # wrong dimension
        with pytest.raises(ValueError):
            model.predict(
                n=self.horizon,
                series=[
                    self.ts_pass_train,
                    self.ts_pass_train.stack(self.ts_pass_train),
                ],
            )

    @pytest.mark.parametrize(
        "config",
        itertools.product(
            [(ConformalNaiveModel, {"quantiles": [0.1, 0.5, 0.9]}, "regression")],
            [
                {"lags_past_covariates": IN_LEN},
                {"lags_future_covariates": (IN_LEN, OUT_LEN)},
                {},
            ],
        ),
    )
    def test_covariates(self, config):
        (model_cls, kwargs, model_type), covs_kwargs = config
        model_fc = LinearRegressionModel(**regr_kwargs, **covs_kwargs)
        # Here we rely on the fact that all non-Dual models currently are Past models
        if model_fc.supports_future_covariates:
            cov_name = "future_covariates"
            is_past = False
        elif model_fc.supports_past_covariates:
            cov_name = "past_covariates"
            is_past = True
        else:
            cov_name = None
            is_past = None

        covariates = [self.time_covariates_train, self.time_covariates_train]
        if cov_name is not None:
            cov_kwargs = {cov_name: covariates}
            cov_kwargs_train = {cov_name: self.time_covariates_train}
            cov_kwargs_notrain = {cov_name: self.time_covariates}
        else:
            cov_kwargs = {}
            cov_kwargs_train = {}
            cov_kwargs_notrain = {}

        model_fc.fit(series=[self.ts_pass_train, self.ts_pass_train_1], **cov_kwargs)

        model = model_cls(model=model_fc, **kwargs)
        if cov_name == "future_covariates":
            assert model.supports_future_covariates
            assert not model.supports_past_covariates
            assert model.uses_future_covariates
            assert not model.uses_past_covariates
        elif cov_name == "past_covariates":
            assert not model.supports_future_covariates
            assert model.supports_past_covariates
            assert not model.uses_future_covariates
            assert model.uses_past_covariates
        else:
            assert not model.supports_future_covariates
            assert not model.supports_past_covariates
            assert not model.uses_future_covariates
            assert not model.uses_past_covariates

        with pytest.raises(ValueError):
            # when model is fit from >1 series, one must provide a series in argument
            model.predict(n=1)

        if cov_name is not None:
            with pytest.raises(ValueError):
                # when model is fit using multiple covariates, covariates are required at prediction time
                model.predict(n=1, series=self.ts_pass_train)

            with pytest.raises(ValueError):
                # when model is fit using covariates, n cannot be greater than output_chunk_length...
                # (for short covariates)
                # past covariates model can predict up until output_chunk_length
                # with train future covariates we cannot predict at all after end of series
                model.predict(
                    n=OUT_LEN + 1 if is_past else 1,
                    series=self.ts_pass_train,
                    **cov_kwargs_train,
                )
        else:
            # model does not support covariates
            with pytest.raises(ValueError):
                model.predict(
                    n=1,
                    series=self.ts_pass_train,
                    past_covariates=self.time_covariates,
                )
            with pytest.raises(ValueError):
                model.predict(
                    n=1,
                    series=self.ts_pass_train,
                    future_covariates=self.time_covariates,
                )

        # ... unless future covariates are provided
        _ = model.predict(
            n=self.horizon, series=self.ts_pass_train, **cov_kwargs_notrain
        )

        pred = model.predict(
            n=self.horizon, series=self.ts_pass_train, **cov_kwargs_notrain
        )
        pred_fc = model_fc.predict(
            n=self.horizon,
            series=self.ts_pass_train,
            **cov_kwargs_notrain,
        )
        fc_columns = likelihood_component_names(
            self.ts_pass_val.columns, quantile_names([0.5])
        )
        np.testing.assert_array_almost_equal(
            pred[fc_columns].all_values(),
            pred_fc.all_values(),
        )

        if cov_name is None:
            return

        # when model is fit using 1 training and 1 covariate series, time series args are optional
        model_fc = LinearRegressionModel(**regr_kwargs, **covs_kwargs)
        model_fc.fit(series=self.ts_pass_train, **cov_kwargs_train)
        model = model_cls(model_fc, **kwargs)

        if is_past:
            # can only predict up until ocl
            with pytest.raises(ValueError):
                _ = model.predict(n=OUT_LEN + 1)
            # wrong covariates dimension
            with pytest.raises(ValueError):
                covs = cov_kwargs_train[cov_name]
                covs = {cov_name: covs.stack(covs)}
                _ = model.predict(n=OUT_LEN + 1, **covs)
            # with past covariates from train we can predict up until output_chunk_length
            pred1 = model.predict(n=OUT_LEN)
            pred2 = model.predict(n=OUT_LEN, series=self.ts_pass_train)
            pred3 = model.predict(n=OUT_LEN, **cov_kwargs_train)
            pred4 = model.predict(
                n=OUT_LEN, **cov_kwargs_train, series=self.ts_pass_train
            )
        else:
            # with future covariates we need additional time steps to predict
            with pytest.raises(ValueError):
                _ = model.predict(n=1)
            with pytest.raises(ValueError):
                _ = model.predict(n=1, series=self.ts_pass_train)
            with pytest.raises(ValueError):
                _ = model.predict(n=1, **cov_kwargs_train)
            with pytest.raises(ValueError):
                _ = model.predict(n=1, **cov_kwargs_train, series=self.ts_pass_train)
            # wrong covariates dimension
            with pytest.raises(ValueError):
                covs = cov_kwargs_notrain[cov_name]
                covs = {cov_name: covs.stack(covs)}
                _ = model.predict(n=OUT_LEN + 1, **covs)
            pred1 = model.predict(n=OUT_LEN, **cov_kwargs_notrain)
            pred2 = model.predict(
                n=OUT_LEN, series=self.ts_pass_train, **cov_kwargs_notrain
            )
            pred3 = model.predict(n=OUT_LEN, **cov_kwargs_notrain)
            pred4 = model.predict(
                n=OUT_LEN, **cov_kwargs_notrain, series=self.ts_pass_train
            )

        assert pred1 == pred2
        assert pred1 == pred3
        assert pred1 == pred4

    @pytest.mark.parametrize(
        "config,ts",
        itertools.product(
            models_cls_kwargs_errs,
            [ts_w_static_cov, ts_shared_static_cov, ts_comps_static_cov],
        ),
    )
    def test_use_static_covariates(self, config, ts):
        """
        Check that both static covariates representations are supported (component-specific and shared)
        for both uni- and multivariate series when fitting the model.
        Also check that the static covariates are present in the forecasted series
        """
        model_cls, kwargs, model_type = config
        model = model_cls(
            train_model(ts, model_type=model_type, quantiles=kwargs["quantiles"]),
            **kwargs,
        )
        assert model.uses_static_covariates
        pred = model.predict(OUT_LEN)
        assert pred.static_covariates is None

    @pytest.mark.parametrize(
        "config",
        itertools.product(
            [True, False],  # univariate series
            [True, False],  # single series
            [True, False],  # use covariates
            [True, False],  # datetime index
            [1, 3, 5],  # different horizons
        ),
    )
    def test_predict(self, config):
        (is_univar, is_single, use_covs, is_datetime, horizon) = config
        series = self.ts_pass_train
        if not is_univar:
            series = series.stack(series)
        if not is_datetime:
            series = TimeSeries.from_values(series.all_values(), columns=series.columns)
        if use_covs:
            pc, fc = series, series
            fc = fc.append_values(fc.values()[: max(horizon, OUT_LEN)])
            if horizon > OUT_LEN:
                pc = pc.append_values(pc.values()[: horizon - OUT_LEN])
            model_kwargs = {
                "lags_past_covariates": IN_LEN,
                "lags_future_covariates": (IN_LEN, OUT_LEN),
            }
        else:
            pc, fc = None, None
            model_kwargs = {}
        if not is_single:
            series = [
                series,
                series.with_columns_renamed(
                    col_names=series.columns.tolist(),
                    col_names_new=(series.columns + "_s2").tolist(),
                ),
            ]
            if use_covs:
                pc = [pc] * 2
                fc = [fc] * 2

        # testing lags_past_covariates None but past_covariates during prediction
        model_instance = LinearRegressionModel(
            lags=IN_LEN, output_chunk_length=OUT_LEN, **model_kwargs
        )
        model_instance.fit(series=series, past_covariates=pc, future_covariates=fc)
        model = ConformalNaiveModel(model_instance, quantiles=q)

        preds = model.predict(
            n=horizon, series=series, past_covariates=pc, future_covariates=fc
        )

        if is_single:
            series = [series]
            preds = [preds]

        for s_, preds_ in zip(series, preds):
            cols_expected = likelihood_component_names(s_.columns, quantile_names(q))
            assert preds_.columns.tolist() == cols_expected
            assert len(preds_) == horizon
            assert preds_.start_time() == s_.end_time() + s_.freq
            assert preds_.freq == s_.freq

    def test_output_chunk_shift(self):
        model_params = {"output_chunk_shift": 1}
        model = ConformalNaiveModel(
            train_model(self.ts_pass_train, model_params=model_params, quantiles=q),
            quantiles=q,
        )
        pred = model.predict(n=1)
        pred_fc = model.model.predict(n=1)

        assert pred_fc.time_index.equals(pred.time_index)
        # the center forecasts must be equal to the forecasting model forecast
        fc_columns = likelihood_component_names(
            self.ts_pass_train.columns, quantile_names([0.5])
        )

        np.testing.assert_array_almost_equal(
            pred[fc_columns].all_values(), pred_fc.all_values()
        )

        pred_cal = model.predict(n=1, cal_series=self.ts_pass_train)
        assert pred_fc.time_index.equals(pred_cal.time_index)
        # the center forecasts must be equal to the forecasting model forecast
        np.testing.assert_array_almost_equal(pred_cal.all_values(), pred.all_values())

    @pytest.mark.parametrize(
        "config",
        list(
            itertools.product(
                [1, 3, 5],  # horizon
                [True, False],  # univariate series
                [True, False],  # single series
                [q, [0.2, 0.3, 0.5, 0.7, 0.8]],
                [
                    (ConformalNaiveModel, "regression"),
                    (ConformalNaiveModel, "regression_prob"),
                    (ConformalQRModel, "regression_qr"),
                ],  # model type
                [True, False],  # symmetric non-conformity score
                [None, 1],  # train length
            )
        ),
    )
    def test_conformal_model_predict_accuracy(self, config):
        """Verifies that naive conformal model computes the correct intervals for:
        - different horizons (smaller, equal, larger than ocl)
        - uni/multivariate series
        - single/multi series
        - single/multi quantile intervals
        - deterministic/probabilistic forecasting model
        - naive conformal and conformalized quantile regression
        - symmetric/asymmetric non-conformity scores

        The naive approach computes it as follows:

        - pred_upper = pred + q_interval(absolute error, past)
        - pred_middle = pred
        - pred_lower = pred - q_interval(absolute error, past)

        Where q_interval(absolute error) is the `q_hi - q_hi` quantile value of all historic absolute errors
        between `pred`, and the target series.
        """
        (
            n,
            is_univar,
            is_single,
            quantiles,
            (model_cls, model_type),
            symmetric,
            cal_length,
        ) = config
        idx_med = quantiles.index(0.5)
        q_intervals = [
            (q_hi, q_lo)
            for q_hi, q_lo in zip(quantiles[:idx_med], quantiles[idx_med + 1 :][::-1])
        ]
        series = self.helper_prepare_series(is_univar, is_single)
        pred_kwargs = (
            {"num_samples": 1000}
            if model_type in ["regression_prob", "regression_qr"]
            else {}
        )

        model_fc = train_model(series, model_type=model_type, quantiles=q)
        model = model_cls(
            model=model_fc,
            quantiles=quantiles,
            symmetric=symmetric,
            cal_length=cal_length,
        )
        pred_fc_list = model.model.predict(n, series=series, **pred_kwargs)
        pred_cal_list = model.predict(n, series=series)
        pred_cal_list_with_cal = model.predict(n, series=series, cal_series=series)

        if issubclass(model_cls, ConformalNaiveModel):
            metric = ae if symmetric else err
            metric_kwargs = {}
        else:
            metric = incs_qr
            metric_kwargs = {"q_interval": q_intervals, "symmetric": symmetric}
        # compute the expected intervals
        residuals_list = model.model.residuals(
            series,
            retrain=False,
            forecast_horizon=n,
            overlap_end=True,
            last_points_only=False,
            stride=1,
            values_only=True,
            metric=metric,
            metric_kwargs=metric_kwargs,
            **pred_kwargs,
        )
        if is_single:
            pred_fc_list = [pred_fc_list]
            pred_cal_list = [pred_cal_list]
            residuals_list = [residuals_list]
            pred_cal_list_with_cal = [pred_cal_list_with_cal]

        for pred_fc, pred_cal, pred_cal_with_cal, residuals in zip(
            pred_fc_list, pred_cal_list, pred_cal_list_with_cal, residuals_list
        ):
            residuals = np.concatenate(residuals[:-1], axis=2)

            pred_vals = pred_fc.all_values()
            pred_vals_expected = self.helper_compute_pred_cal(
                residuals,
                pred_vals,
                n,
                quantiles,
                model_type,
                symmetric,
                cal_length=cal_length,
            )
            self.helper_compare_preds(pred_cal, pred_vals_expected, model_type)
            self.helper_compare_preds(pred_cal_with_cal, pred_vals_expected, model_type)

    @pytest.mark.parametrize(
        "config",
        itertools.product(
            [1, 3, 5],  # horizon
            [True, False],  # univariate series
            [True, False],  # single series,
            [0, 1],  # output chunk shift
            [None, 1],  # train length
            [False, True],  # use covariates
            [q, [0.2, 0.3, 0.5, 0.7, 0.8]],  # quantiles
        ),
    )
    def test_naive_conformal_model_historical_forecasts(self, config):
        """Checks correctness of naive conformal model historical forecasts for:
        - different horizons (smaller, equal and larger the OCL)
        - uni and multivariate series
        - single and multiple series
        - with and without output shift
        - with and without training length
        - with and without covariates in the forecast and calibration sets.
        """
        n, is_univar, is_single, ocs, cal_length, use_covs, quantiles = config
        n_q = len(quantiles)
        half_idx = n_q // 2
        if ocs and n > OUT_LEN:
            # auto-regression not allowed with ocs
            return

        series = self.helper_prepare_series(is_univar, is_single)
        model_params = {"output_chunk_shift": ocs}

        # for covariates, we check that shorter & longer covariates in the calibration set give expected results
        covs_kwargs = {}
        cal_covs_kwargs_overlap = {}
        cal_covs_kwargs_short = {}
        cal_covs_kwargs_exact = {}
        if use_covs:
            model_params["lags_past_covariates"] = regr_kwargs["lags"]
            past_covs = series
            if n > OUT_LEN:
                append_vals = [[[1.0]] * (1 if is_univar else 2)] * (n - OUT_LEN)
                if is_single:
                    past_covs = past_covs.append_values(append_vals)
                else:
                    past_covs = [pc.append_values(append_vals) for pc in past_covs]
            covs_kwargs["past_covariates"] = past_covs
            # produces examples with all points in `overlap_end=True` (last example has no useful information)
            cal_covs_kwargs_overlap["cal_past_covariates"] = past_covs
            # produces one example less (drops the one with unuseful information)
            cal_covs_kwargs_exact["cal_past_covariates"] = (
                past_covs[: -(1 + ocs)]
                if is_single
                else [pc[: -(1 + ocs)] for pc in past_covs]
            )
            # produces another example less (drops the last one which contains useful information)
            cal_covs_kwargs_short["cal_past_covariates"] = (
                past_covs[: -(2 + ocs)]
                if is_single
                else [pc[: -(2 + ocs)] for pc in past_covs]
            )

        # forecasts from forecasting model
        model_fc = train_model(series, model_params=model_params, **covs_kwargs)
        hfc_fc_list = model_fc.historical_forecasts(
            series,
            retrain=False,
            forecast_horizon=n,
            overlap_end=True,
            last_points_only=False,
            stride=1,
            **covs_kwargs,
        )
        # residuals to compute the conformal intervals
        residuals_list = model_fc.residuals(
            series,
            historical_forecasts=hfc_fc_list,
            overlap_end=True,
            last_points_only=False,
            values_only=True,
            metric=ae,  # absolute error
            **covs_kwargs,
        )

        # conformal forecasts
        model = ConformalNaiveModel(
            model=model_fc, quantiles=quantiles, cal_length=cal_length
        )
        # without calibration set
        hfc_conf_list = model.historical_forecasts(
            series=series,
            forecast_horizon=n,
            overlap_end=True,
            last_points_only=False,
            stride=1,
            **covs_kwargs,
        )
        # with calibration set and covariates that can generate all calibration forecasts in the overlap
        hfc_conf_list_with_cal = model.historical_forecasts(
            series=series,
            forecast_horizon=n,
            overlap_end=True,
            last_points_only=False,
            stride=1,
            cal_series=series,
            **covs_kwargs,
            **cal_covs_kwargs_overlap,
        )

        if is_single:
            hfc_conf_list = [hfc_conf_list]
            residuals_list = [residuals_list]
            hfc_conf_list_with_cal = [hfc_conf_list_with_cal]
            hfc_fc_list = [hfc_fc_list]

        # validate computed conformal intervals that did not use a calibration set
        # conformal models start later since they need past residuals as input
        first_fc_idx = len(hfc_fc_list[0]) - len(hfc_conf_list[0])
        for hfc_fc, hfc_conf, hfc_residuals in zip(
            hfc_fc_list, hfc_conf_list, residuals_list
        ):
            for idx, (pred_fc, pred_cal) in enumerate(
                zip(hfc_fc[first_fc_idx:], hfc_conf)
            ):
                # need to ignore additional `ocs` (output shift) residuals
                residuals = np.concatenate(
                    hfc_residuals[: first_fc_idx - ocs + idx], axis=2
                )

                pred_vals = pred_fc.all_values()
                pred_vals_expected = self.helper_compute_pred_cal(
                    residuals,
                    pred_vals,
                    n,
                    quantiles,
                    cal_length=cal_length,
                    model_type="regression",
                    symmetric=True,
                )
                np.testing.assert_array_almost_equal(
                    pred_cal.all_values(), pred_vals_expected
                )

        # validate computed conformal intervals that used a calibration set
        for hfc_conf_with_cal, hfc_conf in zip(hfc_conf_list_with_cal, hfc_conf_list):
            # last forecast with calibration set must be equal to the last without calibration set
            # (since calibration set is the same series)
            assert hfc_conf_with_cal[-1] == hfc_conf[-1]
            hfc_0_vals = hfc_conf_with_cal[0].all_values()
            for hfc_i in hfc_conf_with_cal[1:]:
                hfc_i_vals = hfc_i.all_values()
                for q_idx in range(n_q):
                    np.testing.assert_array_almost_equal(
                        hfc_0_vals[:, half_idx::n_q] - hfc_0_vals[:, q_idx::n_q],
                        hfc_i_vals[:, half_idx::n_q] - hfc_i_vals[:, q_idx::n_q],
                    )

        if use_covs:
            # `cal_covs_kwargs_exact` will not compute the last example in overlap_end (this one has anyways no
            # useful information). Result is expected to be identical to the case when using `cal_covs_kwargs_overlap`
            hfc_conf_list_with_cal_exact = model.historical_forecasts(
                series=series,
                forecast_horizon=n,
                overlap_end=True,
                last_points_only=False,
                stride=1,
                cal_series=series,
                **covs_kwargs,
                **cal_covs_kwargs_exact,
            )

            # `cal_covs_kwargs_short` will compute example less that contains useful information
            hfc_conf_list_with_cal_short = model.historical_forecasts(
                series=series,
                forecast_horizon=n,
                overlap_end=True,
                last_points_only=False,
                stride=1,
                cal_series=series,
                **covs_kwargs,
                **cal_covs_kwargs_short,
            )
            if is_single:
                hfc_conf_list_with_cal_exact = [hfc_conf_list_with_cal_exact]
                hfc_conf_list_with_cal_short = [hfc_conf_list_with_cal_short]

            # must match
            assert hfc_conf_list_with_cal_exact == hfc_conf_list_with_cal

            # second last forecast with shorter calibration set (that has one example less) must be equal to the
            # second last without calibration set
            for hfc_conf_with_cal, hfc_conf in zip(
                hfc_conf_list_with_cal_short, hfc_conf_list
            ):
                assert hfc_conf_with_cal[-2] == hfc_conf[-2]

        # checking that last points only is equal to the last forecasted point
        hfc_lpo_list = model.historical_forecasts(
            series=series,
            forecast_horizon=n,
            overlap_end=True,
            last_points_only=True,
            stride=1,
            **covs_kwargs,
        )
        hfc_lpo_list_with_cal = model.historical_forecasts(
            series=series,
            forecast_horizon=n,
            overlap_end=True,
            last_points_only=True,
            stride=1,
            cal_series=series,
            **covs_kwargs,
            **cal_covs_kwargs_overlap,
        )
        if is_single:
            hfc_lpo_list = [hfc_lpo_list]
            hfc_lpo_list_with_cal = [hfc_lpo_list_with_cal]

        for hfc_lpo, hfc_conf in zip(hfc_lpo_list, hfc_conf_list):
            hfc_conf_lpo = concatenate([hfc[-1:] for hfc in hfc_conf], axis=0)
            assert hfc_lpo == hfc_conf_lpo

        for hfc_lpo, hfc_conf in zip(hfc_lpo_list_with_cal, hfc_conf_list_with_cal):
            hfc_conf_lpo = concatenate([hfc[-1:] for hfc in hfc_conf], axis=0)
            assert hfc_lpo == hfc_conf_lpo

    def test_probabilistic_historical_forecast(self):
        """Checks correctness of naive conformal historical forecast from probabilistic fc model compared to
        deterministic one,
        """
        series = self.helper_prepare_series(False, False)
        # forecasts from forecasting model
        model_det = ConformalNaiveModel(
            train_model(series, model_type="regression", quantiles=q),
            quantiles=q,
        )
        model_prob = ConformalNaiveModel(
            train_model(series, model_type="regression_prob", quantiles=q),
            quantiles=q,
        )
        hfcs_det = model_det.historical_forecasts(
            series,
            forecast_horizon=2,
            last_points_only=True,
            stride=1,
        )
        hfcs_prob = model_prob.historical_forecasts(
            series,
            forecast_horizon=2,
            last_points_only=True,
            stride=1,
        )
        assert isinstance(hfcs_det, list) and len(hfcs_det) == 2
        assert isinstance(hfcs_prob, list) and len(hfcs_prob) == 2
        for hfc_det, hfc_prob in zip(hfcs_det, hfcs_prob):
            assert hfc_det.columns.equals(hfc_prob.columns)
            assert hfc_det.time_index.equals(hfc_prob.time_index)
            self.helper_compare_preds(
                hfc_prob, hfc_det.all_values(), model_type="regression_prob"
            )

    def helper_prepare_series(self, is_univar, is_single):
        series = self.ts_pass_train
        if not is_univar:
            series = series.stack(series + 3.0)
        if not is_single:
            series = [series, series + 5]
        return series

    def helper_compare_preds(self, cp_pred, pred_expected, model_type, tol_rel=0.1):
        if model_type == "regression":
            # deterministic fc model should give almost identical results
            np.testing.assert_array_almost_equal(cp_pred.all_values(), pred_expected)
        else:
            # probabilistic fc models have some randomness
            cp_pred_vals = cp_pred.all_values()
            diffs_rel = np.abs((cp_pred_vals - pred_expected) / pred_expected)
            assert (diffs_rel < tol_rel).all().all()

    @staticmethod
    def helper_compute_pred_cal(
        residuals, pred_vals, n, quantiles, model_type, symmetric, cal_length=None
    ):
        """Generates expected prediction results for naive conformal model from:

        - residuals and predictions from deterministic/probabilistic model
        - any forecast horizon
        - any quantile intervals
        - symmetric/ asymmetric non-conformity scores
        - any train length
        """
        cal_length = cal_length or 0
        n_comps = pred_vals.shape[1]
        half_idx = len(quantiles) // 2

        # get alphas from quantiles (alpha = q_hi - q_lo) per interval
        alphas = np.array(quantiles[half_idx + 1 :][::-1]) - np.array(
            quantiles[:half_idx]
        )
        if not symmetric:
            # asymmetric non-conformity scores look only on one tail -> alpha/2
            alphas = 1 - (1 - alphas) / 2
        if model_type == "regression_prob":
            # naive conformal model converts probabilistic forecasts to median (deterministic)
            pred_vals = np.expand_dims(np.quantile(pred_vals, 0.5, axis=2), -1)
        elif model_type == "regression_qr":
            # conformalized quantile regression consumes quantile forecasts
            pred_vals = np.quantile(pred_vals, quantiles, axis=2).transpose(1, 2, 0)

        is_naive = model_type in ["regression", "regression_prob"]
        pred_expected = []
        for alpha_idx, alpha in enumerate(alphas):
            q_hats = []
            # compute the quantile `alpha` of all past residuals (absolute "per time step" errors between historical
            # forecasts and the target series)
            for idx in range(n):
                res_end = residuals.shape[2] - idx
                if cal_length:
                    res_start = res_end - cal_length
                else:
                    res_start = n - (idx + 1)
                res_n = residuals[idx][:, res_start:res_end]
                if is_naive and symmetric:
                    # identical correction for upper and lower bounds
                    # metric is `ae()`
                    q_hat_n = np.quantile(res_n, q=alpha, axis=1)
                    q_hats.append((-q_hat_n, q_hat_n))
                elif is_naive:
                    # correction separately for upper and lower bounds
                    # metric is `err()`
                    q_hat_hi = np.quantile(res_n, q=alpha, axis=1)
                    q_hat_lo = np.quantile(-res_n, q=alpha, axis=1)
                    q_hats.append((-q_hat_lo, q_hat_hi))
                elif symmetric:  # CQR symmetric
                    # identical correction for upper and lower bounds
                    # metric is `incs_qr(symmetric=True)`
                    q_hat_n = np.quantile(res_n, q=alpha, axis=1)
                    q_hats.append((-q_hat_n, q_hat_n))
                else:  # CQR asymmetric
                    # correction separately for upper and lower bounds
                    # metric is `incs_qr(symmetric=False)`
                    half_idx = len(res_n) // 2

                    # residuals have shape (n components * n intervals * 2)
                    # the factor 2 comes from the metric being computed for lower, and upper bounds separately
                    # (comp_1_qlow_1, comp_1_qlow_2, ... comp_n_qlow_m, comp_1_qhigh_1, ...)
                    q_hat_lo = np.quantile(res_n[:half_idx], q=alpha, axis=1)
                    q_hat_hi = np.quantile(res_n[half_idx:], q=alpha, axis=1)
                    q_hats.append((
                        -q_hat_lo[alpha_idx :: len(alphas)],
                        q_hat_hi[alpha_idx :: len(alphas)],
                    ))
            # bring to shape (horizon, n components, 2)
            q_hats = np.array(q_hats).transpose((0, 2, 1))
            # the prediction interval is given by pred +/- q_hat
            pred_vals_expected = []
            for col_idx in range(n_comps):
                q_col = q_hats[:, col_idx]
                pred_col = pred_vals[:, col_idx]
                if is_naive:
                    # conformal model corrects deterministic predictions
                    idx_q_lo = slice(0, None)
                    idx_q_med = slice(0, None)
                    idx_q_hi = slice(0, None)
                else:
                    # conformal model corrects quantile predictions
                    idx_q_lo = slice(alpha_idx, alpha_idx + 1)
                    idx_q_med = slice(len(alphas), len(alphas) + 1)
                    idx_q_hi = slice(
                        pred_col.shape[1] - (alpha_idx + 1),
                        pred_col.shape[1] - alpha_idx,
                    )
                # correct lower and upper bounds
                pred_col_expected = np.concatenate(
                    [
                        pred_col[:, idx_q_lo] + q_col[:, :1],  # lower quantile
                        pred_col[:, idx_q_med],  # median forecast
                        pred_col[:, idx_q_hi] + q_col[:, 1:],
                    ],  # upper quantile
                    axis=1,
                )
                pred_col_expected = np.expand_dims(pred_col_expected, 1)
                pred_vals_expected.append(pred_col_expected)
            pred_vals_expected = np.concatenate(pred_vals_expected, axis=1)
            pred_expected.append(pred_vals_expected)

        # reorder to have columns going from lowest quantiles to highest per component
        pred_expected_reshaped = []
        for comp_idx in range(n_comps):
            for q_idx in [0, 1, 2]:
                for pred_idx in range(len(pred_expected)):
                    # upper quantiles will have reversed order
                    if q_idx == 2:
                        pred_idx = len(pred_expected) - 1 - pred_idx
                    pred_ = pred_expected[pred_idx][:, comp_idx, q_idx]
                    pred_ = pred_.reshape(-1, 1, 1)

                    # q_hat_idx = q_idx + comp_idx * 3 + alpha_idx * 3 * n_comps
                    pred_expected_reshaped.append(pred_)
                    # only add median quantile once
                    if q_idx == 1:
                        break
        return np.concatenate(pred_expected_reshaped, axis=1)

    @pytest.mark.parametrize(
        "config",
        itertools.product(
            [1, 3, 5],  # horizon
            [0, 1],  # output chunk shift
            [False, True],  # use covariates
        ),
    )
    def test_too_short_input_predict(self, config):
        """Checks conformal model predict with minimum required input and too short input."""
        n, ocs, use_covs = config
        if ocs and n > OUT_LEN:
            return
        icl = IN_LEN
        min_len = icl + ocs + n
        series = tg.linear_timeseries(length=min_len)
        series_train = [tg.linear_timeseries(length=IN_LEN + OUT_LEN + ocs)] * 2

        model_params = {"output_chunk_shift": ocs}
        covs_kwargs = {}
        cal_covs_kwargs = {}
        covs_kwargs_train = {}
        covs_kwargs_too_short = {}
        cal_covs_kwargs_short = {}
        if use_covs:
            model_params["lags_past_covariates"] = regr_kwargs["lags"]
            covs_kwargs_train["past_covariates"] = series_train
            # use shorter covariates, to test whether residuals are still properly extracted
            past_covs = series
            # for auto-regression, we require longer past covariates
            if n > OUT_LEN:
                past_covs = past_covs.append_values([1.0] * (n - OUT_LEN))
            covs_kwargs["past_covariates"] = past_covs
            covs_kwargs_too_short["past_covariates"] = past_covs[:-1]
            # giving covs in calibration set requires one calibration example less
            cal_covs_kwargs["cal_past_covariates"] = past_covs[: -(1 + ocs)]
            cal_covs_kwargs_short["cal_past_covariates"] = past_covs[: -(2 + ocs)]

        model = ConformalNaiveModel(
            train_model(
                series=series_train,
                model_params=model_params,
                **covs_kwargs_train,
            ),
            quantiles=q,
        )

        # prediction works with long enough input
        preds1 = model.predict(n=n, series=series, **covs_kwargs)
        assert not np.isnan(preds1.all_values()).any().any()
        preds2 = model.predict(
            n=n, series=series, **covs_kwargs, cal_series=series, **cal_covs_kwargs
        )
        assert not np.isnan(preds2.all_values()).any().any()
        # series too short: without covariates, make `series` shorter. Otherwise, use the shorter covariates
        series_ = series[:-1] if not use_covs else series

        with pytest.raises(ValueError) as exc:
            _ = model.predict(n=n, series=series_, **covs_kwargs_too_short)
        if not use_covs:
            assert str(exc.value).startswith(
                "Could not build the minimum required calibration input with the provided `cal_series`"
            )
        else:
            # if `past_covariates` are too short, then it raises error from the forecasting_model.predict()
            assert str(exc.value).startswith(
                "The `past_covariates` at list/sequence index 0 are not long enough."
            )

        with pytest.raises(ValueError) as exc:
            _ = model.predict(
                n=n,
                series=series,
                cal_series=series_,
                **covs_kwargs,
                **cal_covs_kwargs_short,
            )
        if not use_covs or n > 1:
            assert str(exc.value).startswith(
                "Could not build the minimum required calibration input with the provided `cal_series`"
            )
        else:
            # if `cal_past_covariates` are too short and `horizon=1`, then it raises error from the forecasting model
            assert str(exc.value).startswith(
                "Cannot build a single input for prediction with the provided model"
            )

    @pytest.mark.parametrize(
        "config",
        itertools.product(
            [False, True],  # last points only
            [False, True],  # overlap end
            [None, 2],  # train length
            [0, 1],  # output chunk shift
            [1, 3, 5],  # horizon
            [True, False],  # use covs
        ),
    )
    def test_too_short_input_hfc(self, config):
        """Checks conformal model historical forecasts with minimum required input and too short input."""
        (
            last_points_only,
            overlap_end,
            cal_length,
            ocs,
            n,
            use_covs,
        ) = config
        if ocs and n > OUT_LEN:
            return

        icl = IN_LEN
        ocl = OUT_LEN
        horizon_ocs = n + ocs
        add_cal_length = cal_length - 1 if cal_length is not None else 0
        # min length to generate 1 conformal forecast
        min_len_val_series = (
            icl + horizon_ocs * (1 + int(not overlap_end)) + add_cal_length
        )

        series_train = [tg.linear_timeseries(length=icl + ocl + ocs)] * 2
        series = tg.linear_timeseries(length=min_len_val_series)

        # define cal series to get the minimum required cal set
        if overlap_end:
            # with overlap_end `series` has the exact length to generate one forecast after the end of the input series
            # Therefore, `series` has already the minimum length for one calibrated forecast
            cal_series = series
        else:
            # without overlap_end, we use a shorter input, since the last forecast is within the input series
            # (it generates more residuals with useful information than the minimum requirements)
            cal_series = series[:-horizon_ocs]

        series_with_cal = series[: -(horizon_ocs + add_cal_length)]

        model_params = {"output_chunk_shift": ocs}
        covs_kwargs_train = {}
        covs_kwargs = {}
        covs_with_cal_kwargs = {}
        cal_covs_kwargs = {}
        covs_kwargs_short = {}
        cal_covs_kwargs_short = {}
        if use_covs:
            model_params["lags_past_covariates"] = regr_kwargs["lags"]
            covs_kwargs_train["past_covariates"] = series_train

            # `- horizon_ocs` to generate forecasts extending up until end of target series
            if not overlap_end:
                past_covs = series[:-horizon_ocs]
            else:
                past_covs = series

            # calibration set is always generated internally with `overlap_end=True`
            # make shorter to not compute residuals without useful information
            cal_past_covs = cal_series[: -(1 + ocs)]

            # last_points_only requires `horizon` residuals less
            if last_points_only:
                cal_past_covs = cal_past_covs[: (-(n - 1) or None)]

            # for auto-regression, we require longer past covariates
            if n > OUT_LEN:
                past_covs = past_covs.append_values([1.0] * (n - OUT_LEN))
                cal_past_covs = cal_past_covs.append_values([1.0] * (n - OUT_LEN))

            # covariates lengths to generate exactly one forecast
            covs_kwargs["past_covariates"] = past_covs
            # giving a calibration set requires fewer forecasts
            covs_with_cal_kwargs["past_covariates"] = past_covs[:-horizon_ocs]
            cal_covs_kwargs["cal_past_covariates"] = cal_past_covs

            # use too short covariates to check that errors are raised
            covs_kwargs_short["past_covariates"] = covs_kwargs["past_covariates"][:-1]
            cal_covs_kwargs_short["cal_past_covariates"] = cal_covs_kwargs[
                "cal_past_covariates"
            ][:-1]

        model = ConformalNaiveModel(
            train_model(
                series=series_train,
                model_params=model_params,
                **covs_kwargs_train,
            ),
            quantiles=q,
            cal_length=cal_length,
        )

        hfc_kwargs = {
            "last_points_only": last_points_only,
            "overlap_end": overlap_end,
            "forecast_horizon": n,
        }
        # prediction works with long enough input
        hfcs = model.historical_forecasts(
            series=series,
            **covs_kwargs,
            **hfc_kwargs,
        )
        hfcs_cal = model.historical_forecasts(
            series=series_with_cal,
            cal_series=cal_series,
            **covs_with_cal_kwargs,
            **cal_covs_kwargs,
            **hfc_kwargs,
        )
        if last_points_only:
            hfcs = [hfcs]
            hfcs_cal = [hfcs_cal]

        assert len(hfcs) == len(hfcs_cal) == 1
        for hfc, hfc_cal in zip(hfcs, hfcs_cal):
            assert not np.isnan(hfc.all_values()).any().any()
            assert not np.isnan(hfc_cal.all_values()).any().any()

        # input too short: without covariates, make `series` shorter. Otherwise, use the shorter covariates
        series_ = series[:-1] if not use_covs else series
        cal_series_ = cal_series[:-1] if not use_covs else cal_series

        with pytest.raises(ValueError) as exc:
            _ = model.historical_forecasts(
                series=series_,
                **covs_kwargs_short,
                **hfc_kwargs,
            )
        assert str(exc.value).startswith(
            "Could not build the minimum required calibration input with the provided `series` and `*_covariates`"
        )

        with pytest.raises(ValueError) as exc:
            _ = model.historical_forecasts(
                series=series_with_cal,
                cal_series=cal_series_,
                **covs_with_cal_kwargs,
                **cal_covs_kwargs_short,
                **hfc_kwargs,
            )
        if (not use_covs or n > 1 or (cal_length or 1) > 1) and not (
            last_points_only and use_covs and cal_length is None
        ):
            assert str(exc.value).startswith(
                "Could not build the minimum required calibration input with the provided `cal_series`"
            )
        else:
            assert str(exc.value).startswith(
                "Cannot build a single input for prediction with the provided model"
            )

    @pytest.mark.parametrize("quantiles", [[0.1, 0.5, 0.9], [0.1, 0.3, 0.5, 0.7, 0.9]])
    def test_backtest_and_residuals(self, quantiles):
        """Residuals and backtest are already tested for quantile, and interval metrics based on stochastic or quantile
        forecasts. So, a simple check that they give expected results should be enough.
        """
        n_q = len(quantiles)
        half_idx = n_q // 2
        q_interval = [
            (q_lo, q_hi)
            for q_lo, q_hi in zip(quantiles[:half_idx], quantiles[half_idx + 1 :][::-1])
        ]
        lpo = False

        # series long enough for 2 hfcs
        series = self.helper_prepare_series(True, True).append_values([0.1])
        # conformal model
        model = ConformalNaiveModel(model=train_model(series), quantiles=quantiles)

        hfc = model.historical_forecasts(
            series=series, forecast_horizon=5, last_points_only=lpo
        )
        bt = model.backtest(
            series=series, historical_forecasts=hfc, last_points_only=lpo, metric=mic
        )
        # default backtest is equal to backtest with metric kwargs
        np.testing.assert_array_almost_equal(
            bt,
            model.backtest(
                series=series,
                historical_forecasts=hfc,
                last_points_only=lpo,
                metric=mic,
                metric_kwargs={"q_interval": q_interval},
            ),
        )
        np.testing.assert_array_almost_equal(
            mic(
                [series] * len(hfc),
                hfc,
                q_interval=q_interval,
                series_reduction=np.mean,
            ),
            bt,
        )

        residuals = model.residuals(
            series=series, historical_forecasts=hfc, last_points_only=lpo, metric=ic
        )
        # default residuals is equal to residuals with metric kwargs
        assert residuals == model.residuals(
            series=series,
            historical_forecasts=hfc,
            last_points_only=lpo,
            metric=ic,
            metric_kwargs={"q_interval": q_interval},
        )
        expected_vals = ic([series] * len(hfc), hfc, q_interval=q_interval)
        expected_residuals = []
        for vals, hfc_ in zip(expected_vals, hfc):
            expected_residuals.append(
                TimeSeries.from_times_and_values(
                    times=hfc_.time_index,
                    values=vals,
                    columns=likelihood_component_names(
                        series.components, quantile_interval_names(q_interval)
                    ),
                )
            )
        assert residuals == expected_residuals
