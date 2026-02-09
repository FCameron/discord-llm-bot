import importlib

import pytest


# Autouse fixture – inject the LlamaGPT module into each test module.
@pytest.fixture(autouse=True)
def _inject_LlamaGPT(request):
    module = importlib.import_module("LlamaGPT")
    setattr(request.module, "LlamaGPT", module)


# Explicit fixture – returned when a test declares a `LlamaGPT` parameter.
@pytest.fixture
def LlamaGPT():
    """Return the imported LlamaGPT module."""
    return importlib.import_module("LlamaGPT")
