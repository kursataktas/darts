# flake8: noqa
import os
from typing import Any, Dict

import torch
from pytorch_lightning.callbacks import EarlyStopping

from darts.models import (
    DLinearModel,
    LightGBMModel,
    LinearRegressionModel,
    NHiTSModel,
    NLinearModel,
    TCNModel,
)

# DEEP LEARNING MODELS
torch_early_stopper = EarlyStopping(
    "val_loss", min_delta=0.001, patience=3, verbose=True
)

CALLBACKS = [torch_early_stopper]

# detect if a GPU is available
if torch.cuda.is_available():
    PL_TRAINER_KWARGS = {
        "accelerator": "gpu",
        "gpus": -1,
        "auto_select_gpus": True,
        "callbacks": CALLBACKS,
    }
else:
    PL_TRAINER_KWARGS = {"callbacks": CALLBACKS}


def compute_lag_length(len_train, len_val, lag_ratio):
    lag_length = int(lag_ratio * len_train)
    lag_length = min([lag_length, int(len_val / 2)])
    lag_length = max([lag_length, 0])

    return lag_length


def NHiTSModelBuilder(
    input_chunk_length,
    out_len,
    num_stacks,
    num_blocks,
    num_layers,
    layer_widths,
    lr,
    pooling_kernel_sizes,
    n_freq_downsample,
    dropout,
    activation,
    MaxPool1d,
    add_encoders,
    encoders,
    fixed_params,
    likelihood=None,
    callbacks=None,
    work_dir=None,
):

    if callbacks is not None:
        CALLBACKS.extend(callbacks)
        PL_TRAINER_KWARGS["callbacks"] = CALLBACKS

    model = NHiTSModel(
        input_chunk_length=input_chunk_length,
        output_chunk_length=out_len,
        num_stacks=num_stacks,
        num_blocks=num_blocks,
        num_layers=num_layers,
        layer_widths=layer_widths,
        pooling_kernel_sizes=pooling_kernel_sizes,
        n_freq_downsample=n_freq_downsample,
        dropout=dropout,
        activation=activation,
        MaxPool1d=MaxPool1d,
        batch_size=fixed_params["BATCH_SIZE"],
        n_epochs=fixed_params["MAX_N_EPOCHS"],
        nr_epochs_val_period=fixed_params["NR_EPOCHS_VAL_PERIOD"],
        optimizer_kwargs={"lr": lr},
        add_encoders=encoders if add_encoders else None,
        likelihood=likelihood,
        pl_trainer_kwargs=PL_TRAINER_KWARGS,
        model_name=NHiTSModel.__name__,
        force_reset=True,
        save_checkpoints=True,
        work_dir=os.path.join(os.getcwd()) if work_dir is None else work_dir,
    )

    return model


def NLinearModelBuilder(
    input_chunk_length,
    out_len,
    const_init,
    lr,
    shared_weights,
    normalize,
    add_encoders,
    encoders,
    fixed_params,
    likelihood=None,
    callbacks=None,
    work_dir=None,
):
    if callbacks is not None:
        CALLBACKS.extend(callbacks)
        PL_TRAINER_KWARGS["callbacks"] = CALLBACKS

    model = NLinearModel(
        input_chunk_length=input_chunk_length,
        output_chunk_length=out_len,
        shared_weights=shared_weights,
        const_init=const_init,
        normalize=normalize,
        batch_size=fixed_params["BATCH_SIZE"],
        n_epochs=fixed_params["MAX_N_EPOCHS"],
        nr_epochs_val_period=fixed_params["NR_EPOCHS_VAL_PERIOD"],
        optimizer_kwargs={"lr": lr},
        add_encoders=encoders if add_encoders else None,
        likelihood=likelihood,
        pl_trainer_kwargs=PL_TRAINER_KWARGS,
        model_name=NLinearModel.__name__,
        force_reset=True,
        save_checkpoints=True,
        work_dir=os.path.join(os.getcwd()) if work_dir is None else work_dir,
    )

    return model


def DLinearModelBuilder(
    input_chunk_length,
    out_len,
    const_init,
    lr,
    kernel_size,
    shared_weights,
    add_encoders,
    encoders,
    fixed_params,
    likelihood=None,
    callbacks=None,
    work_dir=None,
):
    if callbacks is not None:
        CALLBACKS.extend(callbacks)
        PL_TRAINER_KWARGS["callbacks"] = CALLBACKS

    model = DLinearModel(
        input_chunk_length=input_chunk_length,
        output_chunk_length=out_len,
        shared_weights=shared_weights,
        kernel_size=kernel_size,
        const_init=const_init,
        batch_size=fixed_params["BATCH_SIZE"],
        n_epochs=fixed_params["MAX_N_EPOCHS"],
        nr_epochs_val_period=fixed_params["NR_EPOCHS_VAL_PERIOD"],
        optimizer_kwargs={"lr": lr},
        add_encoders=encoders if add_encoders else None,
        likelihood=likelihood,
        pl_trainer_kwargs=PL_TRAINER_KWARGS,
        model_name=DLinearModel.__name__,
        force_reset=True,
        save_checkpoints=True,
        work_dir=os.path.join(os.getcwd()) if work_dir is None else work_dir,
    )

    return model


def TCNModelBuilder(
    input_chunk_length,
    out_len,
    kernel_size,
    num_filters,
    weight_norm,
    dilation_base,
    dropout,
    lr,
    add_encoders,
    encoders,
    fixed_params,
    likelihood=None,
    callbacks=None,
    work_dir=None,
):
    if callbacks is not None:
        CALLBACKS.extend(callbacks)
        PL_TRAINER_KWARGS["callbacks"] = CALLBACKS

    # build the model
    model = TCNModel(
        input_chunk_length=input_chunk_length,
        output_chunk_length=out_len,
        batch_size=fixed_params["BATCH_SIZE"],
        n_epochs=fixed_params["MAX_N_EPOCHS"],
        nr_epochs_val_period=fixed_params["NR_EPOCHS_VAL_PERIOD"],
        kernel_size=kernel_size,
        num_filters=num_filters,
        weight_norm=weight_norm,
        dilation_base=dilation_base,
        dropout=dropout,
        optimizer_kwargs={"lr": lr},
        add_encoders=encoders if add_encoders else None,
        likelihood=likelihood,
        pl_trainer_kwargs=PL_TRAINER_KWARGS,
        model_name=TCNModel.__name__,
        force_reset=True,
        save_checkpoints=True,
        work_dir=os.path.join(os.getcwd()) if work_dir is None else work_dir,
    )

    return model


# ML MODELS
def LGBMModelBuilder(
    lags,
    out_len,
    add_encoders,
    num_iterations,
    num_leaves,
    learning_rate,
    max_bin,
    boosting,
    fixed_params,
    encoders=None,
    work_dir=None,
):

    kwargs = {
        "num_leaves": num_leaves,
        "learning_rate": learning_rate,
        "num_iterations": num_iterations,
        "max_bin": max_bin,
        "boosting": boosting,
        "early_stopping_rounds": 2,
    }

    model = LightGBMModel(
        lags=lags,
        output_chunk_length=out_len,
        lags_future_covariates=(lags, out_len) if add_encoders else None,
        add_encoders=encoders if add_encoders else None,
        random_state=fixed_params["RANDOM_STATE"],
        multi_models=False,
        **kwargs
    )
    return model


def LinearRegressionModelBuilder(
    optuna_params: Dict[str, Any],
    len_train: int,
    len_val: int,
    out_len: int,
    fixed_params: Dict[str, Any],
    encoders: Dict[str, Any] = None,
    work_dir: str = None,
):
    lag_length = compute_lag_length(len_train, len_val, optuna_params["lags_ratio"])
    model = LinearRegressionModel(
        lags=lag_length,
        lags_future_covariates=(lag_length, out_len)
        if optuna_params["add_encoders"]
        else None,
        output_chunk_length=out_len,
        add_encoders=encoders if optuna_params["add_encoders"] else None,
        random_state=fixed_params["RANDOM_STATE"],
        multi_models=True,
    )

    return model


MODEL_BUILDERS = {
    TCNModel.__name__: TCNModelBuilder,
    DLinearModel.__name__: DLinearModelBuilder,
    NLinearModel.__name__: NLinearModelBuilder,
    NHiTSModel.__name__: NHiTSModelBuilder,
    LightGBMModel.__name__: LGBMModelBuilder,
    LinearRegressionModel.__name__: LinearRegressionModelBuilder,
}