"""API configuration projections must contain JSON-serializable nested values."""
from app.backtesting.component_config import resolve_components
from app.strategies.schemas import StrategyBacktestWorkspaceResponse


def test_workspace_serializes_real_component_descriptors():
    components = resolve_components()
    response = StrategyBacktestWorkspaceResponse(
        strategy={}, published_revisions=[], slippage_models=[], formal_gate={},
        component_options={kind: [value] for kind, value in components.items() if kind not in ('analyzer', 'slippage_model')},
        runs={'items': []},
    )
    encoded = response.model_dump_json()
    assert 'properties' in encoded
    # Editing an API projection must not mutate the registry's frozen schema.
    components['execution_model']['parameter_schema']['properties'].clear()
    assert resolve_components()['execution_model']['parameter_schema']['properties']
