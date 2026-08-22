"""Offline tests for common.iter_config_references.

No Home Assistant, no token, no network. These pin the classifier's behaviour,
because the scanners' correctness now rests entirely on it: a scanner that
misclassifies everything as a type would report a clean bill of health, which
looks identical to success from the outside.
"""

import common


def classify(config):
    """Convenience: {value: kind} for a config fragment."""
    return dict(common.iter_config_references(config))


class TestTriggerTypes:
    """The bug this module exists to prevent."""

    def test_trigger_type_is_not_an_entity(self):
        config = {
            "triggers": [
                {
                    "trigger": "motion.detected",
                    "target": {"entity_id": "binary_sensor.hallway"},
                }
            ]
        }
        refs = classify(config)
        assert refs["motion.detected"] == "type"
        assert refs["binary_sensor.hallway"] == "entity"

    def test_legacy_platform_key_is_also_a_type(self):
        assert classify({"platform": "state"}) == {}
        assert classify({"platform": "device.turned_on"}) == {
            "device.turned_on": "type"
        }

    def test_unknown_future_trigger_types_need_no_allowlist(self):
        """The point of reading the key: a type nobody has heard of still
        classifies correctly, where an allowlist would have to be updated."""
        config = {"triggers": [{"trigger": "something.entirely_new"}]}
        assert classify(config) == {"something.entirely_new": "type"}


class TestServiceCalls:
    def test_action_string_is_a_service(self):
        config = {"actions": [{"action": "light.turn_on"}]}
        assert classify(config) == {"light.turn_on": "service"}

    def test_legacy_service_key_is_a_service(self):
        config = {"action": [{"service": "light.turn_off"}]}
        assert classify(config) == {"light.turn_off": "service"}

    def test_action_as_a_sequence_yields_nothing_itself(self):
        """`action` names both a service call and an action sequence. The value
        type is what separates them: a list is a sequence, and only the strings
        inside it are references."""
        config = {
            "action": [
                {"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}
            ]
        }
        refs = classify(config)
        assert refs == {"light.turn_on": "service", "light.kitchen": "entity"}


class TestEntities:
    def test_entity_id_as_a_list(self):
        config = {"entity_id": ["light.a", "light.b"]}
        assert classify(config) == {"light.a": "entity", "light.b": "entity"}

    def test_deeply_nested_entity_is_found(self):
        config = {"cards": [{"cards": [{"entity": "sensor.buried"}]}]}
        assert classify(config) == {"sensor.buried": "entity"}

    def test_a_list_inherits_its_parent_key(self):
        """A list under `trigger:` is still trigger types, not entities."""
        config = {"trigger": ["motion.detected", "occupancy.cleared"]}
        assert classify(config) == {
            "motion.detected": "type",
            "occupancy.cleared": "type",
        }


class TestNonReferences:
    def test_undotted_values_are_not_references(self):
        config = {"name": "Hallway", "icon": "mdi", "mode": "restart"}
        assert classify(config) == {}

    def test_values_with_spaces_are_not_references(self):
        assert classify({"alias": "Turn on the light. Then wait."}) == {}

    def test_empty_and_scalar_configs_do_not_raise(self):
        assert classify({}) == {}
        assert classify([]) == {}
        assert list(common.iter_config_references(None)) == []
        assert list(common.iter_config_references(42)) == []


class TestKnownLimitation:
    def test_templates_are_not_inspected(self):
        """Documented, not desired: the reference is inside the template rather
        than being the value. Closing this surfaces new findings and is a
        separate change -- see the docstring on iter_config_references."""
        config = {"value_template": "{{ states('sensor.foo') }}"}
        assert classify(config) == {}
