"""Single-device Lightning models with bounded dense minibatches from sparse shards."""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import lightning.pytorch as pl
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, IterableDataset

from scripts.lamda.models.base import FittedModel, TrainingData


class SparseBatches(IterableDataset):
    """Read training shards only; densify at most batch_size rows at once.

    No worker processes or distributed devices: each epoch visits each selected
    row exactly once. Shards remain sparse until minibatch slicing is complete.
    """
    # Store training-only input access and the batch bound used when converting sparse rows to
    # tensors.
    def __init__(self, data: TrainingData, batch_size: int, *, benign_only=False, seed=42):
        self.data, self.batch_size = data, batch_size
        self.benign_only, self.seed, self.epoch = benign_only, seed, 0

    # Reshuffle deterministically each epoch and restrict autoencoder fitting to benign
    # examples.
    def __iter__(self):
        rng = np.random.default_rng(self.seed + self.epoch)
        self.epoch += 1
        from scripts.lamda.data import cached
        for matrix, metadata in cached(self.data.directory, rng.permutation(self.data.shards).tolist()):
            labels = metadata["label"].to_numpy()
            indices = np.flatnonzero(labels == 0) if self.benign_only else np.arange(len(labels))
            rng.shuffle(indices)
            for start in range(0, len(indices), self.batch_size):
                selected = indices[start:start + self.batch_size]
                x = torch.from_numpy(matrix[selected].toarray().astype(np.float32, copy=False))
                y = torch.from_numpy(labels[selected].astype(np.float32, copy=True))
                yield x, y


# Build a scalar-logit classifier or a symmetric reconstruction network from the selected
# dimensions.
def build_network(kind, n_features, hidden_dims, latent_dim, dropout):
    if kind == "mlp":
        dimensions = [n_features, *hidden_dims, 1]
    else:
        dimensions = [n_features, *hidden_dims, latent_dim, *reversed(hidden_dims), n_features]
    layers = []
    for index, (left, right) in enumerate(zip(dimensions, dimensions[1:])):
        layers.append(nn.Linear(left, right))
        if index < len(dimensions) - 2:
            layers.append(nn.ReLU())
            if kind == "mlp":
                layers.append(nn.Dropout(dropout))
    return nn.Sequential(*layers)


class MalwareModule(pl.LightningModule):
    """MLP predicts a label logit; autoencoder predicts one logit per input bit."""
    # Save reconstruction parameters and register class weights as device-aware model state.
    def __init__(self, kind, n_features, hidden_dims, latent_dim, dropout, learning_rate,
                 class_weights=(1.0, 1.0)):
        super().__init__()
        self.save_hyperparameters()
        self.network = build_network(kind, n_features, hidden_dims, latent_dim, dropout)
        self.register_buffer("class_weights", torch.tensor(class_weights, dtype=torch.float32))

    # Leave logits untransformed so each loss and score function applies its own interpretation.
    def forward(self, x):
        return self.network(x)

    # Use reconstruction BCE for anomaly learning or class-weighted BCE for supervised malware
    # detection.
    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self(x)
        if self.hparams.kind == "autoencoder":
            loss = F.binary_cross_entropy_with_logits(logits, x)
        else:
            losses = F.binary_cross_entropy_with_logits(logits.squeeze(-1), y, reduction="none")
            loss = (losses * self.class_weights[y.long()]).mean()
        if not torch.isfinite(loss):
            raise ValueError("Neural training loss is not finite")
        self.log("train_loss", loss, on_step=False, on_epoch=True, batch_size=len(x), logger=False)
        return loss

    # Use the configured learning rate for all trainable network parameters.
    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate)


class EpochTracking(pl.Callback):
    """Use the shared run, without giving Lightning ownership of its lifetime."""
    # Reuse the orchestrator-owned tracking run and maintain a per-epoch sample count.
    def __init__(self, run):
        self.run, self.rows = run, 0

    # Reset counts so metrics describe this epoch rather than cumulative visits.
    def on_train_epoch_start(self, trainer, module):
        self.rows = 0

    # Count actual rows received, including a potentially short final minibatch.
    def on_train_batch_end(self, trainer, module, outputs, batch, batch_idx):
        self.rows += len(batch[0])

    # Fail empty training populations and report epoch loss through the common tracking
    # interface.
    def on_train_epoch_end(self, trainer, module):
        if not self.rows:
            raise ValueError("No training rows reached the Lightning model")
        loss = float(trainer.callback_metrics["train_loss"].detach().cpu())
        self.run.metrics({"train.loss": loss, "train.rows": self.rows}, step=trainer.current_epoch)
        print(f"{module.hparams.kind}: epoch {trainer.current_epoch + 1}, loss={loss:.6f}", flush=True)


@dataclass
class NeuralPredictor:
    """Portable CPU inference payload; only configuration and NumPy weights persist."""
    configuration: dict
    weights: dict
    batch_size: int
    _network: object = field(default=None, init=False, repr=False)

    # Exclude the lazily rebuilt Torch module from the portable serialized predictor.
    def __getstate__(self):
        return {**self.__dict__, "_network": None}

    # Rebuild CPU weights on demand and score bounded minibatches without enabling gradients.
    def predict_scores(self, matrix):
        if self._network is None:
            self._network = build_network(**self.configuration)
            self._network.load_state_dict({k: torch.from_numpy(v) for k, v in self.weights.items()})
            self._network.eval()
        result = []
        with torch.inference_mode():
            for start in range(0, matrix.shape[0], self.batch_size):
                x = torch.from_numpy(matrix[start:start + self.batch_size].toarray().astype(np.float32, copy=False))
                logits = self._network(x)
                scores = (F.binary_cross_entropy_with_logits(logits, x, reduction="none").mean(dim=1)
                          if self.configuration["kind"] == "autoencoder"
                          else torch.sigmoid(logits.squeeze(-1)))
                result.append(scores.numpy())
        return np.concatenate(result) if result else np.empty(0, dtype=np.float32)


# Adapt both neural model families to the training-only backend contract.
class LightningAdapter:
    backend = "lamda_iceberg_lightning_v1"

    # Validate architecture and execution settings before creating a trainer or allocating
    # tensors.
    def __init__(self, *, kind, hidden_dims=(256, 64), latent_dim=32, dropout=0.1,
                 learning_rate=0.001, neural_batch_size=256, accelerator="cpu"):
        if kind not in ("mlp", "autoencoder"):
            raise ValueError("Unknown Lightning model")
        if not hidden_dims or any(type(n) is not int or n < 1 for n in hidden_dims):
            raise ValueError("hidden_dims must contain positive integers")
        if type(latent_dim) is not int or latent_dim < 1 or type(neural_batch_size) is not int or neural_batch_size < 1:
            raise ValueError("latent_dim and neural_batch_size must be positive integers")
        if not math.isfinite(learning_rate) or learning_rate <= 0 or not 0 <= dropout < 1:
            raise ValueError("Invalid learning rate or dropout")
        if accelerator not in ("cpu", "gpu", "auto"):
            raise ValueError("accelerator must be cpu, gpu or auto")
        self.name = kind
        self.parameters = dict(hidden_dims=list(hidden_dims), latent_dim=latent_dim,
                               dropout=dropout if kind == "mlp" else 0.0,
                               learning_rate=learning_rate, neural_batch_size=neural_batch_size,
                               accelerator=accelerator)

    # Return a copy so orchestration cannot mutate the adapter configuration.
    def candidates(self):
        return [self.parameters.copy()]

    # Train on one device, log epochs to the shared run, and export a portable predictor plus
    # checkpoint.
    def fit(self, data, parameters, *, epochs, seed, run, directory):
        pl.seed_everything(seed, workers=True)
        p = parameters
        count = np.asarray(data.class_counts)
        weights = count.sum() / (2.0 * count) if self.name == "mlp" else np.ones(2)
        run.params({"fit_population": "benign_training_only" if self.name == "autoencoder" else "all_training_rows",
                    "class_weights": weights.tolist(), "devices": 1, "precision": "32-true"})
        module = MalwareModule(self.name, data.n_features, p["hidden_dims"], p["latent_dim"],
                               p["dropout"], p["learning_rate"], weights.tolist())
        batches = SparseBatches(data, p["neural_batch_size"], benign_only=self.name == "autoencoder", seed=seed)
        trainer = pl.Trainer(max_epochs=epochs, accelerator=p["accelerator"], devices=1,
                             precision="32-true", deterministic=True, logger=False,
                             callbacks=[EpochTracking(run)], enable_checkpointing=False,
                             enable_progress_bar=False, enable_model_summary=False,
                             num_sanity_val_steps=0, default_root_dir=str(directory))
        trainer.fit(module, train_dataloaders=DataLoader(batches, batch_size=None, num_workers=0))
        if trainer.interrupted:
            raise KeyboardInterrupt("Lightning training interrupted")
        run.params({"resolved_device": str(trainer.strategy.root_device), "torch_version": torch.__version__,
                    "lightning_version": pl.__version__})
        checkpoint = directory / "model.ckpt"
        trainer.save_checkpoint(checkpoint)
        run.artifact(checkpoint)
        config = dict(kind=self.name, n_features=data.n_features, hidden_dims=p["hidden_dims"],
                      latent_dim=p["latent_dim"], dropout=p["dropout"])
        predictor = NeuralPredictor(config,
                                    {k: v.detach().cpu().numpy().copy() for k, v in module.network.state_dict().items()},
                                    p["neural_batch_size"])
        return FittedModel(predictor, "anomaly" if self.name == "autoencoder" else "probability")
