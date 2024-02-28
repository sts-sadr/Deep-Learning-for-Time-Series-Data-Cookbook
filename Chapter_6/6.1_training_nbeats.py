import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from gluonts.dataset.repository.datasets import get_dataset

import torch
import lightning.pytorch as pl
from lightning.pytorch.callbacks import EarlyStopping
from pytorch_forecasting import NBeats, TimeSeriesDataSet
from matplotlib import pyplot as plt

dataset = get_dataset("nn5_daily_without_missing", regenerate=False)

N_LAGS = 7
HORIZON = 7


class LogTransformation:

    @staticmethod
    def transform(x):
        xt = np.sign(x) * np.log(np.abs(x) + 1)

        return xt

    @staticmethod
    def inverse_transform(xt):
        x = np.sign(xt) * (np.exp(np.abs(xt)) - 1)

        return x


class LocalScaler:

    def __init__(self):
        self.scalers = {}

    def fit(self, df: pd.DataFrame):
        df = df.copy()
        df_g = df.groupby("group_id")
        for g, df_ in df_g:
            scl = StandardScaler()
            scl.fit(df_[["value"]])

            self.scalers[g] = scl

    def transform(self, df: pd.DataFrame):
        df = df.copy()
        df["value"] = LogTransformation.transform(df["value"])

        df_g = df.groupby("group_id")
        transf_df_l = []
        for g, df_ in df_g:
            df_[["value"]] = self.scalers[g].transform(df_[["value"]])

            transf_df_l.append(df_)

        transf_df = pd.concat(transf_df_l)
        transf_df = transf_df.sort_index()

        return transf_df


class GlobalDataModule(pl.LightningDataModule):
    def __init__(
        self, data, n_lags: int, horizon: int, test_size: float, batch_size: int
    ):
        super().__init__()

        self.data = data
        self.batch_size = batch_size
        self.test_size = test_size
        self.n_lags = n_lags
        self.horizon = horizon

        self.training = None
        self.validation = None
        self.test = None
        self.predict_set = None

        self.target_scaler = LocalScaler()

    def setup(self, stage=None):
        data_list = list(self.data.train)

        data_list = [
            pd.Series(
                ts["target"],
                index=pd.date_range(
                    start=ts["start"].to_timestamp(),
                    freq=ts["start"].freq,
                    periods=len(ts["target"]),
                ),
            )
            for ts in data_list
        ]

        tseries_df = pd.concat(data_list, axis=1)
        tseries_df["time_index"] = np.arange(tseries_df.shape[0])

        ts_df = tseries_df.melt("time_index")
        ts_df = ts_df.rename(columns={"variable": "group_id"})

        unique_times = ts_df["time_index"].sort_values().unique()

        tr_ind, ts_ind = train_test_split(
            unique_times, test_size=self.test_size, shuffle=False
        )

        tr_ind, vl_ind = train_test_split(tr_ind, test_size=0.1, shuffle=False)

        training_df = ts_df.loc[ts_df["time_index"].isin(tr_ind), :]
        validation_df = ts_df.loc[ts_df["time_index"].isin(vl_ind), :]
        test_df = ts_df.loc[ts_df["time_index"].isin(ts_ind), :]

        self.target_scaler.fit(training_df)

        training_df = self.target_scaler.transform(training_df)
        validation_df = self.target_scaler.transform(validation_df)
        test_df = self.target_scaler.transform(test_df)

        self.training = TimeSeriesDataSet(
            data=training_df,
            time_idx="time_index",
            target="value",
            group_ids=["group_id"],
            max_encoder_length=self.n_lags,
            max_prediction_length=self.horizon,
            time_varying_unknown_reals=["value"],
            target_normalizer=None,
        )

        self.validation = TimeSeriesDataSet.from_dataset(self.training, validation_df)
        self.test = TimeSeriesDataSet.from_dataset(self.training, test_df)
        self.predict_set = TimeSeriesDataSet.from_dataset(
            self.training, ts_df, predict=True
        )

    def train_dataloader(self):
        return self.training.to_dataloader(batch_size=self.batch_size, shuffle=False)

    def val_dataloader(self):
        return self.validation.to_dataloader(batch_size=self.batch_size, shuffle=False)

    def test_dataloader(self):
        return self.test.to_dataloader(batch_size=self.batch_size, shuffle=False)

    def predict_dataloader(self):
        return self.predict_set.to_dataloader(batch_size=1, shuffle=False)


datamodule = GlobalDataModule(
    data=dataset, n_lags=N_LAGS, horizon=HORIZON, batch_size=256, test_size=0.2
)

datamodule.setup()

# SETTING UP MODEL

early_stop_callback = EarlyStopping(
    monitor="val_loss", min_delta=1e-4, patience=10, verbose=False, mode="min"
)

trainer = pl.Trainer(
    max_epochs=2,
    accelerator="auto",
    enable_model_summary=True,
    gradient_clip_val=0.01,
    callbacks=[early_stop_callback],
)

model = NBeats.from_dataset(
    dataset=datamodule.training,
    stack_types=["trend", "seasonality"],
    num_blocks=[3, 3],  # The number of blocks per stack.
    num_block_layers=[4, 4],  # Number of fully connected layers
    widths=[256, 2048],
    sharing=[True],
    backcast_loss_ratio=1.0,
)

trainer.fit(
    model,
    train_dataloaders=datamodule.train_dataloader(),
    val_dataloaders=datamodule.val_dataloader(),
)

best_model_path = trainer.checkpoint_callback.best_model_path
best_model = NBeats.load_from_checkpoint(best_model_path)

actuals = torch.cat(
    [y[0] for x, y in iter(datamodule.test.to_dataloader(batch_size=1, shuffle=False))]
)
predictions = best_model.predict(
    datamodule.test.to_dataloader(batch_size=1, shuffle=False)
)
(actuals - predictions.cpu()).abs().mean()

# INTERPRETATION

raw_predictions = best_model.predict(
    datamodule.val_dataloader(), mode="raw", return_x=True
)

best_model.plot_interpretation(x=raw_predictions[1], output=raw_predictions[0], idx=0)
plt.show()
