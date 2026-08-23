"""Contract tests for the versioned component registry."""

import unittest

from app.backtesting.registry import (
    ComponentRegistry,
    ComponentRegistryEntry,
    DuplicateRegistryEntryError,
    RegistryError,
    UnknownComponentError,
    build_default_component_registry,
)


def entry(
    key: str = "demo",
    version: int = 1,
    *,
    name_zh: str = "演示组件",
    name_en: str = "Demo Component",
    capabilities: dict | None = None,
) -> ComponentRegistryEntry:
    return ComponentRegistryEntry(
        component_kind="timing_policy",
        key=key,
        version=version,
        name_zh=name_zh,
        name_en=name_en,
        factory=lambda parameters: object(),
        parameter_schema={"type": "object"},
        capabilities=capabilities or {},
    )


class RegistryContractTests(unittest.TestCase):
    def test_duplicate_key_version_is_rejected(self) -> None:
        registry = ComponentRegistry()
        registry.register(entry())

        with self.assertRaises(DuplicateRegistryEntryError):
            registry.register(entry(version=1))

    def test_renamed_display_names_keep_the_stable_key_registrable_at_next_version(
        self,
    ) -> None:
        registry = ComponentRegistry()
        registry.register(entry(name_zh="旧中文名", name_en="Old Name"))
        # Renaming is a new version under the same stable key, never an
        # overwrite of the existing (key, version).
        registry.register(entry(version=2, name_zh="新中文名", name_en="New Name"))

        renamed = registry.resolve("demo", 2)
        self.assertEqual(renamed.name_zh, "新中文名")
        self.assertEqual(renamed.key, "demo")

    def test_unknown_version_never_falls_back_to_the_latest(self) -> None:
        registry = ComponentRegistry()
        registry.register(entry())
        registry.register(entry(version=2))

        with self.assertRaises(UnknownComponentError) as context:
            registry.resolve("demo", 3)
        self.assertEqual(context.exception.details["known_versions"], [1, 2])

        with self.assertRaises(UnknownComponentError):
            registry.resolve("missing", 1)

    def test_display_name_is_chinese_then_english_in_full_width_parens(
        self,
    ) -> None:
        registry = build_default_component_registry()

        for item in registry.entries():
            self.assertEqual(
                item.display_name,
                f"{item.name_zh}（{item.name_en}）",
            )

    def test_describe_payload_hides_python_class_and_module_paths(self) -> None:
        registry = build_default_component_registry()

        for item in registry.entries():
            payload = item.describe()
            for field in (
                "component_kind",
                "key",
                "version",
                "name_zh",
                "name_en",
                "display_name",
                "parameter_schema",
                "capabilities",
            ):
                self.assertIn(field, payload)
            rendered = repr(payload)
            self.assertNotIn("app.backtesting", rendered)
            self.assertNotIn("factory", rendered)

    def test_describe_payload_is_directly_json_serializable(self) -> None:
        import json

        registry = build_default_component_registry()

        for item in registry.entries():
            payload = item.describe()
            # API surfaces serialize descriptors directly: no frozen
            # container may leak into the payload.
            rendered = json.dumps(payload, ensure_ascii=False)
            self.assertIn(item.display_name, rendered)

    def test_capability_requirements_are_declared_and_validated(self) -> None:
        registry = ComponentRegistry()
        registry.register(
            entry(
                capabilities={
                    "frequency": "1d",
                    "supported_order_types": ("market",),
                    "settlement_timing": "t_plus_1_before_open_match",
                }
            )
        )

        # Scalar equality and sequence membership both validate.
        registry.require_capabilities(
            "demo",
            1,
            {
                "frequency": "1d",
                "supported_order_types": ("market",),
            },
        )
        with self.assertRaises(RegistryError):
            registry.require_capabilities("demo", 1, {"frequency": "1m"})
        with self.assertRaises(RegistryError):
            registry.require_capabilities("demo", 1, {"limit_orders": True})

    def test_entry_validation_rejects_malformed_input(self) -> None:
        with self.assertRaises(RegistryError):
            entry(key="Bad-Key")
        with self.assertRaises(RegistryError):
            entry(version=0)
        with self.assertRaises(RegistryError):
            entry(name_zh=" ")
        with self.assertRaises(RegistryError):
            ComponentRegistryEntry(
                component_kind="timing_policy",
                key="demo",
                version=1,
                name_zh="演示组件",
                name_en="Demo Component",
                factory="not-callable",
            )


class DefaultRegistryTests(unittest.TestCase):
    def test_first_version_entries_are_registered_with_documented_names(
        self,
    ) -> None:
        registry = build_default_component_registry()

        timing = registry.resolve("after_close_to_next_open", 1)
        self.assertEqual(timing.name_zh, "收盘后决策、次日开盘成交")
        self.assertEqual(
            timing.name_en, "After-Close Decision to Next-Open Execution"
        )
        self.assertEqual(
            timing.capabilities["settlement_timing"],
            "t_plus_1_before_open_match",
        )

        execution = registry.resolve("bar_market", 1)
        self.assertEqual(execution.name_zh, "Bar 市价撮合")
        self.assertEqual(execution.name_en, "Bar Market Execution")
        self.assertIn("market", execution.capabilities["supported_order_types"])

        interpreter = registry.resolve("target_weights", 1)
        self.assertEqual(interpreter.name_zh, "目标权重")
        self.assertEqual(interpreter.name_en, "Target Weights")

    def test_factories_construct_working_components(self) -> None:
        from app.backtesting.execution import BarMarketExecutionModel
        from app.backtesting.runtime import TargetWeightsInterpreter
        from app.backtesting.timing import AfterCloseToNextOpenV1

        registry = build_default_component_registry()

        self.assertIsInstance(
            registry.resolve("after_close_to_next_open", 1).construct({}),
            AfterCloseToNextOpenV1,
        )
        model = registry.resolve("bar_market", 1).construct(
            {
                "slippage_bps": 0,
                "price_tick": "0.01",
                "commission_rate": "0.0003",
                "commission_minimum": "5",
            }
        )
        self.assertIsInstance(model, BarMarketExecutionModel)
        self.assertEqual(model.model_key, "bar_market")
        self.assertEqual(model.model_version, 1)
        self.assertIsInstance(
            registry.resolve("target_weights", 1).construct({}),
            TargetWeightsInterpreter,
        )

    def test_bar_market_factory_refuses_implicit_default_fees(self) -> None:
        registry = build_default_component_registry()

        with self.assertRaises(RegistryError) as context:
            registry.resolve("bar_market", 1).construct({"price_tick": "0.01"})
        self.assertEqual(
            context.exception.details["missing_parameters"],
            ["commission_rate", "commission_minimum"],
        )

    def test_api_descriptor_shape_matches_documented_contract(self) -> None:
        registry = build_default_component_registry()

        payload = registry.resolve("after_close_to_next_open", 1).describe()
        self.assertEqual(payload["key"], "after_close_to_next_open")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["name_zh"], "收盘后决策、次日开盘成交")
        self.assertEqual(
            payload["name_en"],
            "After-Close Decision to Next-Open Execution",
        )
        self.assertEqual(
            payload["display_name"],
            "收盘后决策、次日开盘成交（After-Close Decision to Next-Open Execution）",
        )


if __name__ == "__main__":
    unittest.main()
