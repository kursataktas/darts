import os
from copy import deepcopy
from itertools import product
from unittest.mock import ANY, patch

import numpy as np
import pandas as pd
import pytest
from sklearn.preprocessing import MaxAbsScaler

from darts.dataprocessing.pipeline import Pipeline
from darts.dataprocessing.transformers import (
    FittableDataTransformer,
    InvertibleDataTransformer,
    Scaler,
)
from darts.datasets import AirPassengersDataset
from darts.metrics import mape
from darts.tests.conftest import TORCH_AVAILABLE, tfm_kwargs
from darts.utils import timeseries_generation as tg
from darts.utils.timeseries_generation import linear_timeseries

if not TORCH_AVAILABLE:
    pytest.skip(
        f"Torch not available. {__name__} tests will be skipped.",
        allow_module_level=True,
    )
import torch

from darts.models import (
    BlockRNNModel,
    DLinearModel,
    GlobalNaiveAggregate,
    GlobalNaiveDrift,
    GlobalNaiveSeasonal,
    NBEATSModel,
    NLinearModel,
    RNNModel,
    TCNModel,
    TFTModel,
    TiDEModel,
    TransformerModel,
    TSMixerModel,
)
from darts.models.forecasting.torch_forecasting_model import (
    DualCovariatesTorchModel,
    MixedCovariatesTorchModel,
    PastCovariatesTorchModel,
)
from darts.utils.likelihood_models import GaussianLikelihood

IN_LEN = 24
OUT_LEN = 12
models_cls_kwargs_errs = [
    (
        BlockRNNModel,
        {
            "model": "RNN",
            "hidden_dim": 10,
            "n_rnn_layers": 1,
            "batch_size": 32,
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        110.0,
    ),
    (
        RNNModel,
        {
            "model": "RNN",
            "training_length": IN_LEN + OUT_LEN,
            "hidden_dim": 10,
            "batch_size": 32,
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        150.0,
    ),
    (
        RNNModel,
        {
            "training_length": IN_LEN + OUT_LEN,
            "n_epochs": 10,
            "likelihood": GaussianLikelihood(),
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        80.0,
    ),
    (
        TCNModel,
        {
            "n_epochs": 10,
            "batch_size": 32,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        60.0,
    ),
    (
        TransformerModel,
        {
            "d_model": 16,
            "nhead": 2,
            "num_encoder_layers": 2,
            "num_decoder_layers": 2,
            "dim_feedforward": 16,
            "batch_size": 32,
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        60.0,
    ),
    (
        NBEATSModel,
        {
            "num_stacks": 4,
            "num_blocks": 1,
            "num_layers": 2,
            "layer_widths": 12,
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        140.0,
    ),
    (
        TFTModel,
        {
            "hidden_size": 16,
            "lstm_layers": 1,
            "num_attention_heads": 4,
            "add_relative_index": True,
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        70.0,
    ),
    (
        NLinearModel,
        {
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        50.0,
    ),
    (
        DLinearModel,
        {
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        55.0,
    ),
    (
        TiDEModel,
        {
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        40.0,
    ),
    (
        TSMixerModel,
        {
            "n_epochs": 10,
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        60.0,
    ),
    (
        GlobalNaiveAggregate,
        {
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        22,
    ),
    (
        GlobalNaiveDrift,
        {
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        17,
    ),
    (
        GlobalNaiveSeasonal,
        {
            "pl_trainer_kwargs": tfm_kwargs["pl_trainer_kwargs"],
        },
        39,
    ),
]


class TestGlobalForecastingModels:
    # forecasting horizon used in runnability tests
    forecasting_horizon = 12

    np.random.seed(42)
    torch.manual_seed(42)

    # some arbitrary static covariates
    static_covariates = pd.DataFrame([[0.0, 1.0]], columns=["st1", "st2"])

    # real timeseries for functionality tests
    ts_passengers = (
        AirPassengersDataset().load().with_static_covariates(static_covariates)
    )
    scaler = Scaler()
    ts_passengers = scaler.fit_transform(ts_passengers)
    ts_pass_train, ts_pass_val = ts_passengers[:-36], ts_passengers[-36:]

    # an additional noisy series
    ts_pass_train_1 = ts_pass_train + 0.01 * tg.gaussian_timeseries(
        length=len(ts_pass_train),
        freq=ts_pass_train.freq_str,
        start=ts_pass_train.start_time(),
    )

    # an additional time series serving as covariates
    year_series = tg.datetime_attribute_timeseries(ts_passengers, attribute="year")
    month_series = tg.datetime_attribute_timeseries(ts_passengers, attribute="month")
    scaler_dt = Scaler()
    time_covariates = scaler_dt.fit_transform(year_series.stack(month_series))
    time_covariates_train, time_covariates_val = (
        time_covariates[:-36],
        time_covariates[-36:],
    )

    # an artificial time series that is highly dependent on covariates
    ts_length = 400
    split_ratio = 0.6
    sine_1_ts = tg.sine_timeseries(length=ts_length)
    sine_2_ts = tg.sine_timeseries(length=ts_length, value_frequency=0.05)
    sine_3_ts = tg.sine_timeseries(
        length=ts_length, value_frequency=0.003, value_amplitude=5
    )
    linear_ts = tg.linear_timeseries(length=ts_length, start_value=3, end_value=8)

    covariates = sine_3_ts.stack(sine_2_ts).stack(linear_ts)
    covariates_past, _ = covariates.split_after(split_ratio)

    target = sine_1_ts + sine_2_ts + linear_ts + sine_3_ts
    target_past, target_future = target.split_after(split_ratio)

    # various ts with different static covariates representations
    ts_w_static_cov = tg.linear_timeseries(length=80).with_static_covariates(
        pd.Series([1, 2])
    )
    ts_shared_static_cov = ts_w_static_cov.stack(tg.sine_timeseries(length=80))
    ts_comps_static_cov = ts_shared_static_cov.with_static_covariates(
        pd.DataFrame([[0, 1], [2, 3]], columns=["st1", "st2"])
    )

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_save_model_parameters(self, config):
        # model creation parameters were saved before. check if re-created model has same params as original
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        assert model._model_params, model.untrained_model()._model_params

    @pytest.mark.parametrize(
        "model",
        [
            RNNModel(
                input_chunk_length=4,
                hidden_dim=10,
                batch_size=32,
                n_epochs=10,
                **tfm_kwargs,
            ),
            TCNModel(
                input_chunk_length=4,
                output_chunk_length=3,
                n_epochs=10,
                batch_size=32,
                **tfm_kwargs,
            ),
            GlobalNaiveSeasonal(
                input_chunk_length=4,
                output_chunk_length=3,
                **tfm_kwargs,
            ),
        ],
    )
    def test_save_load_model(self, tmpdir_module, model):
        # check if save and load methods work and if loaded model creates same forecasts as original model
        cwd = os.getcwd()
        os.chdir(tmpdir_module)
        model_path_str = type(model).__name__
        full_model_path_str = os.path.join(tmpdir_module, model_path_str)

        model.fit(self.ts_pass_train)
        model_prediction = model.predict(self.forecasting_horizon)

        # test save
        model.save()
        model.save(model_path_str)

        assert os.path.exists(full_model_path_str)
        assert (
            len([
                p
                for p in os.listdir(tmpdir_module)
                if p.startswith(type(model).__name__)
            ])
            == 4
        )

        # test load
        loaded_model = type(model).load(model_path_str)

        assert model_prediction == loaded_model.predict(self.forecasting_horizon)

        os.chdir(cwd)

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_single_ts(self, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN,
            output_chunk_length=OUT_LEN,
            random_state=0,
            **kwargs,
        )
        model.fit(self.ts_pass_train)
        pred = model.predict(n=36)
        mape_err = mape(self.ts_pass_val, pred)
        assert mape_err < err, (
            f"Model {model_cls} produces errors too high (one time "
            f"series). Error = {mape_err}"
        )
        assert pred.static_covariates.equals(self.ts_passengers.static_covariates)

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_multi_ts(self, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN,
            output_chunk_length=OUT_LEN,
            random_state=0,
            **kwargs,
        )
        model.fit([self.ts_pass_train, self.ts_pass_train_1])
        with pytest.raises(ValueError):
            # when model is fit from >1 series, one must provide a series in argument
            model.predict(n=1)
        pred = model.predict(n=36, series=self.ts_pass_train)
        mape_err = mape(self.ts_pass_val, pred)
        assert mape_err < err, (
            f"Model {model_cls} produces errors too high (several time "
            f"series). Error = {mape_err}"
        )

        # check prediction for several time series
        pred_list = model.predict(
            n=36, series=[self.ts_pass_train, self.ts_pass_train_1]
        )
        assert (
            len(pred_list) == 2
        ), f"Model {model_cls} did not return a list of prediction"
        for pred in pred_list:
            mape_err = mape(self.ts_pass_val, pred)
            assert mape_err < err, (
                f"Model {model_cls} produces errors too high (several time series 2). "
                f"Error = {mape_err}"
            )

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_covariates(self, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN,
            output_chunk_length=OUT_LEN,
            random_state=0,
            **kwargs,
        )

        # Here we rely on the fact that all non-Dual models currently are Past models
        if model.supports_future_covariates:
            cov_name = "future_covariates"
            is_past = False
        elif model.supports_past_covariates:
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

        model.fit(series=[self.ts_pass_train, self.ts_pass_train_1], **cov_kwargs)

        if cov_name is None:
            with pytest.raises(ValueError):
                model.untrained_model().fit(
                    series=[self.ts_pass_train, self.ts_pass_train_1],
                    past_covariates=covariates,
                )
            with pytest.raises(ValueError):
                model.untrained_model().fit(
                    series=[self.ts_pass_train, self.ts_pass_train_1],
                    future_covariates=covariates,
                )
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
                    n=13 if is_past else 1,
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
        _ = model.predict(n=13, series=self.ts_pass_train, **cov_kwargs_notrain)

        pred = model.predict(n=12, series=self.ts_pass_train, **cov_kwargs_notrain)
        mape_err = mape(self.ts_pass_val, pred)
        assert mape_err < err, (
            f"Model {model_cls} produces errors too high (several time "
            f"series with covariates). Error = {mape_err}"
        )

        # when model is fit using 1 training and 1 covariate series, time series args are optional
        if model.supports_probabilistic_prediction:
            return
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        model.fit(series=self.ts_pass_train, **cov_kwargs_train)
        if is_past:
            # with past covariates from train we can predict up until output_chunk_length
            pred1 = model.predict(1)
            pred2 = model.predict(1, series=self.ts_pass_train)
            pred3 = model.predict(1, **cov_kwargs_train)
            pred4 = model.predict(1, **cov_kwargs_train, series=self.ts_pass_train)
        else:
            # with future covariates we need additional time steps to predict
            with pytest.raises(ValueError):
                _ = model.predict(1)
            with pytest.raises(ValueError):
                _ = model.predict(1, series=self.ts_pass_train)
            with pytest.raises(ValueError):
                _ = model.predict(1, **cov_kwargs_train)
            with pytest.raises(ValueError):
                _ = model.predict(1, **cov_kwargs_train, series=self.ts_pass_train)

            pred1 = model.predict(1, **cov_kwargs_notrain)
            pred2 = model.predict(1, series=self.ts_pass_train, **cov_kwargs_notrain)
            pred3 = model.predict(1, **cov_kwargs_notrain)
            pred4 = model.predict(1, **cov_kwargs_notrain, series=self.ts_pass_train)

        assert pred1 == pred2
        assert pred1 == pred3
        assert pred1 == pred4

    def test_future_covariates(self):
        # models with future covariates should produce better predictions over a long forecasting horizon
        # than a model trained with no covariates

        model = TCNModel(
            input_chunk_length=50,
            output_chunk_length=5,
            n_epochs=20,
            random_state=0,
            **tfm_kwargs,
        )
        model.fit(series=self.target_past)
        long_pred_no_cov = model.predict(n=160)

        model = TCNModel(
            input_chunk_length=50,
            output_chunk_length=5,
            n_epochs=20,
            random_state=0,
            **tfm_kwargs,
        )
        model.fit(series=self.target_past, past_covariates=self.covariates_past)
        long_pred_with_cov = model.predict(n=160, past_covariates=self.covariates)
        assert mape(self.target_future, long_pred_no_cov) > mape(
            self.target_future, long_pred_with_cov
        ), "Models with future covariates should produce better predictions."

        # block models can predict up to self.output_chunk_length points beyond the last future covariate...
        model.predict(n=165, past_covariates=self.covariates)

        # ... not more
        with pytest.raises(ValueError):
            model.predict(n=166, series=self.ts_pass_train)

        # recurrent models can only predict data points for time steps where future covariates are available
        model = RNNModel(12, n_epochs=1, **tfm_kwargs)
        model.fit(series=self.target_past, future_covariates=self.covariates_past)
        model.predict(n=160, future_covariates=self.covariates)
        with pytest.raises(ValueError):
            model.predict(n=161, future_covariates=self.covariates)

    @pytest.mark.parametrize(
        "model_cls,ts",
        product(
            [TFTModel, DLinearModel, NLinearModel, TiDEModel, TSMixerModel],
            [ts_w_static_cov, ts_shared_static_cov, ts_comps_static_cov],
        ),
    )
    def test_use_static_covariates(self, model_cls, ts):
        """
        Check that both static covariates representations are supported (component-specific and shared)
        for both uni- and multivariate series when fitting the model.
        Also check that the static covariates are present in the forecasted series
        """
        model = model_cls(
            input_chunk_length=IN_LEN,
            output_chunk_length=OUT_LEN,
            random_state=0,
            use_static_covariates=True,
            n_epochs=1,
            **tfm_kwargs,
        )
        # must provide mandatory future_covariates to TFTModel
        model.fit(
            series=ts,
            future_covariates=(
                self.sine_1_ts if model.supports_future_covariates else None
            ),
        )
        pred = model.predict(OUT_LEN)
        assert pred.static_covariates.equals(ts.static_covariates)

    def test_batch_predictions(self):
        # predicting multiple time series at once needs to work for arbitrary batch sizes
        # univariate case
        targets_univar = [
            self.target_past,
            self.target_past[:60],
            self.target_past[:80],
        ]
        self._batch_prediction_test_helper_function(targets_univar)

        # multivariate case
        targets_multivar = [tgt.stack(tgt) for tgt in targets_univar]
        self._batch_prediction_test_helper_function(targets_multivar)

    def _batch_prediction_test_helper_function(self, targets):
        epsilon = 1e-4
        model = TCNModel(
            input_chunk_length=50,
            output_chunk_length=10,
            n_epochs=10,
            random_state=0,
            **tfm_kwargs,
        )
        model.fit(series=targets[0], past_covariates=self.covariates_past)
        preds_default = model.predict(
            n=160,
            series=targets,
            past_covariates=[self.covariates] * len(targets),
            batch_size=None,
        )

        # make batch size large enough to test stacking samples
        for batch_size in range(1, 4 * len(targets)):
            preds = model.predict(
                n=160,
                series=targets,
                past_covariates=[self.covariates] * len(targets),
                batch_size=batch_size,
            )
            for i in range(len(targets)):
                assert sum(sum((preds[i] - preds_default[i]).values())) < epsilon

    def test_predict_from_dataset_unsupported_input(self):
        # an exception should be thrown if an unsupported type is passed
        unsupported_type = "unsupported_type"
        # just need to test this with one model
        model_cls, kwargs, err = models_cls_kwargs_errs[0]
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        model.fit([self.ts_pass_train, self.ts_pass_train_1])

        with pytest.raises(ValueError):
            model.predict_from_dataset(n=1, input_series_dataset=unsupported_type)

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_prediction_with_different_n(self, config):
        # test model predictions for n < out_len, n == out_len and n > out_len
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        assert isinstance(
            model,
            (
                PastCovariatesTorchModel,
                DualCovariatesTorchModel,
                MixedCovariatesTorchModel,
            ),
        ), "unit test not yet defined for the given {X}CovariatesTorchModel."

        if model.supports_past_covariates and model.supports_future_covariates:
            past_covs, future_covs = None, self.covariates
        elif model.supports_past_covariates:
            past_covs, future_covs = self.covariates, None
        elif model.supports_future_covariates:
            past_covs, future_covs = None, self.covariates
        else:
            past_covs, future_covs = None, None

        model.fit(
            self.target_past,
            past_covariates=past_covs,
            future_covariates=future_covs,
            epochs=1,
        )

        # test prediction for n < out_len, n == out_len and n > out_len
        for n in [OUT_LEN - 1, OUT_LEN, 2 * OUT_LEN - 1]:
            pred = model.predict(
                n=n, past_covariates=past_covs, future_covariates=future_covs
            )
            assert len(pred) == n

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_same_result_with_different_n_jobs(self, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )

        multiple_ts = [self.ts_pass_train] * 10

        model.fit(multiple_ts)

        # safe random state for two successive identical predictions
        if model.supports_probabilistic_prediction:
            random_state = deepcopy(model._random_instance)
        else:
            random_state = None

        pred1 = model.predict(n=36, series=multiple_ts, n_jobs=1)

        if random_state is not None:
            model._random_instance = random_state

        pred2 = model.predict(
            n=36, series=multiple_ts, n_jobs=-1
        )  # assuming > 1 core available in the machine
        assert (
            pred1 == pred2
        ), "Model {} produces different predictions with different number of jobs"

    @patch(
        "darts.models.forecasting.torch_forecasting_model.TorchForecastingModel._init_trainer"
    )
    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_fit_with_constr_epochs(self, init_trainer, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        if not model._requires_training:
            return
        multiple_ts = [self.ts_pass_train] * 10
        model.fit(multiple_ts)

        init_trainer.assert_called_with(
            max_epochs=kwargs["n_epochs"], trainer_params=ANY
        )

    @patch(
        "darts.models.forecasting.torch_forecasting_model.TorchForecastingModel._init_trainer"
    )
    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_fit_with_fit_epochs(self, init_trainer, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        multiple_ts = [self.ts_pass_train] * 10
        epochs = 3

        model.fit(multiple_ts, epochs=epochs)
        init_trainer.assert_called_with(max_epochs=epochs, trainer_params=ANY)

        model.total_epochs = epochs
        # continue training
        model.fit(multiple_ts, epochs=epochs)
        init_trainer.assert_called_with(max_epochs=epochs, trainer_params=ANY)

    @patch(
        "darts.models.forecasting.torch_forecasting_model.TorchForecastingModel._init_trainer"
    )
    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_fit_from_dataset_with_epochs(self, init_trainer, config):
        model_cls, kwargs, err = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )
        multiple_ts = [self.ts_pass_train] * 10
        train_dataset = model._build_train_dataset(
            multiple_ts,
            past_covariates=None,
            future_covariates=None,
            sample_weight=None,
            max_samples_per_ts=None,
        )
        epochs = 3

        model.fit_from_dataset(train_dataset, epochs=epochs)
        init_trainer.assert_called_with(max_epochs=epochs, trainer_params=ANY)

        # continue training
        model.fit_from_dataset(train_dataset, epochs=epochs)
        init_trainer.assert_called_with(max_epochs=epochs, trainer_params=ANY)

    @pytest.mark.parametrize("config", models_cls_kwargs_errs)
    def test_predit_after_fit_from_dataset(self, config):
        model_cls, kwargs, _ = config
        model = model_cls(
            input_chunk_length=IN_LEN, output_chunk_length=OUT_LEN, **kwargs
        )

        multiple_ts = [self.ts_pass_train] * 2
        train_dataset = model._build_train_dataset(
            multiple_ts,
            past_covariates=None,
            future_covariates=None,
            sample_weight=None,
            max_samples_per_ts=None,
        )
        model.fit_from_dataset(train_dataset, epochs=1)

        # test predict() works after fit_from_dataset()
        model.predict(n=1, series=multiple_ts[0])

    def test_sample_smaller_than_batch_size(self):
        """
        Checking that the TorchForecastingModels do not crash even if the number of available samples for training
        is strictly lower than the selected batch_size
        """
        # TS with 50 timestamps. TorchForecastingModels will use the SequentialDataset for producing training
        # samples, which means we will have 50 - 22 - 2 + 1 = 27 samples, which is < 32 (batch_size). The model
        # should still train on those samples and not crash in any way
        ts = linear_timeseries(start_value=0, end_value=1, length=50)

        model = RNNModel(
            input_chunk_length=20,
            output_chunk_length=2,
            n_epochs=2,
            batch_size=32,
            **tfm_kwargs,
        )
        model.fit(ts)

    def test_max_samples_per_ts(self):
        """
        Checking that we can fit TorchForecastingModels with max_samples_per_ts, without crash
        """

        ts = linear_timeseries(start_value=0, end_value=1, length=50)

        model = RNNModel(
            input_chunk_length=20,
            output_chunk_length=2,
            n_epochs=2,
            batch_size=32,
            **tfm_kwargs,
        )

        model.fit(ts, max_samples_per_ts=5)

    def test_residuals(self):
        """
        Torch models should not fail when computing residuals on a series
        long enough to accommodate at least one training sample.
        """
        ts = linear_timeseries(start_value=0, end_value=1, length=38)

        model = NBEATSModel(
            input_chunk_length=24,
            output_chunk_length=12,
            num_stacks=2,
            num_blocks=1,
            num_layers=1,
            layer_widths=2,
            n_epochs=2,
            **tfm_kwargs,
        )

        res = model.residuals(ts)
        assert len(res) == 38 - (24 + 12)

    @pytest.mark.parametrize(
        "params",
        product(
            [
                (
                    {"series": sine_3_ts + 3},
                    {"series": Scaler()},
                ),
                (
                    {"series": sine_3_ts + 3, "past_covariates": sine_1_ts + 3},
                    {"past_covariates": Scaler()},
                ),
                (
                    {"series": sine_3_ts + 3, "future_covariates": sine_1_ts + 3},
                    {"future_covariates": Scaler(scaler=MaxAbsScaler())},
                ),
                (
                    {
                        "series": sine_3_ts + 3,
                        "past_covariates": sine_2_ts + 3,
                        "future_covariates": sine_1_ts + 3,
                    },
                    {"series": Scaler(), "past_covariates": Scaler()},
                ),
            ],
            [True, False],
        ),
    )
    def test_historical_forecasts_with_scaler(self, params):
        """Apply manually the scaler on the target and covariates to compare with automatic scaling

        Historical forecasts contains only one horizon to faciliate manual scaling
        """
        (ts, hf_scaler), retrain = params
        ocl = 6
        model = DLinearModel(
            input_chunk_length=4,
            output_chunk_length=ocl,
            n_epochs=2,
            random_state=13,
            **tfm_kwargs,
        )
        # pre-train on the entire unscaled target, overfitting/accuracy is not important
        if not retrain:
            model.fit(**ts)

        hf_args = {
            "start": -ocl,
            "start_format": "position",
            "forecast_horizon": ocl,
            "stride": 1,
            "retrain": retrain,
            "overlap_end": False,
            "last_points_only": False,
            "verbose": False,
            "enable_optimization": False,
        }

        # un-transformed series, scaler applied within the method
        hf_auto = model.historical_forecasts(
            **ts,
            **hf_args,
            data_transformers=hf_scaler,
        )[0]

        hf_auto_pipeline = model.historical_forecasts(
            **ts,
            **hf_args,
            data_transformers={
                key_: Pipeline([val_]) for key_, val_ in hf_scaler.items()
            },
        )[0]

        # verify that the results are identical when using single Scaler or a Pipeline
        assert hf_auto.time_index.equals(hf_auto_pipeline.time_index)
        np.testing.assert_almost_equal(
            hf_auto.values(),
            hf_auto_pipeline.values(),
        )

        # manually scale the series
        ts_scaled = deepcopy(ts)
        for ts_name in hf_scaler:
            if isinstance(hf_scaler[ts_name], FittableDataTransformer):
                if ts_name == "series" or ts_name == "past_covariates":
                    tmp_ts = ts_scaled[ts_name][:-ocl]
                else:
                    tmp_ts = ts_scaled[ts_name][: -ocl + max(0, model.extreme_lags[5])]
                hf_scaler[ts_name].fit(tmp_ts)
            # apply the scaler on the whole series
            ts_scaled[ts_name] = hf_scaler[ts_name].transform(ts_scaled[ts_name])

        # manually generate the last forecast horizon
        series = ts_scaled.pop("series")[:-ocl]
        if retrain:
            model = model.untrained_model()
            model.fit(series=series, **ts_scaled)
        hf_manual = model.predict(n=ocl, series=series, **ts_scaled)

        # scale back the forecasts
        if isinstance(hf_scaler.get("series"), InvertibleDataTransformer):
            hf_manual = hf_scaler["series"].inverse_transform(hf_manual)

        # verify that automatic and manual pre-scaling produce identical forecasts
        assert hf_auto.time_index.equals(hf_manual.time_index)
        np.testing.assert_almost_equal(
            hf_auto.values(),
            hf_manual.values(),
        )
