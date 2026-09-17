"""Register model factories here; orchestration and tracking are model independent."""
from scripts.lamda.models.sgd import SGDAdapter


def _lightning(kind, **options):
    try:
        from scripts.lamda.models.lightning import LightningAdapter
    except ImportError as exc:
        raise ImportError("Neural models require: uv sync --extra ml --extra lightning") from exc
    return LightningAdapter(kind=kind, **options)


MODEL_FACTORIES = {
    "sgd": SGDAdapter,
    "mlp": lambda **options: _lightning("mlp", **options),
    "autoencoder": lambda **options: _lightning("autoencoder", **options),
}


def create_adapter(name, **options):
    if name not in MODEL_FACTORIES:
        raise ValueError(f"Unknown model {name!r}; choose from {', '.join(MODEL_FACTORIES)}")
    return MODEL_FACTORIES[name](**options)
