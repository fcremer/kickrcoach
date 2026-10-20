"""HR boundaries are user configuration, never derived physiological thresholds."""
import pytest
from kickrcoach.cli import Config
from kickrcoach.zones import HeartRateZones


@pytest.mark.parametrize('values', [(), (100,120,140,160)])
def test_valid_zone_configuration(values):
    assert HeartRateZones(values).boundaries == values
    Config(hr_zone_boundaries=list(values)).validate()


@pytest.mark.parametrize('values', [(100,), (100,120,140), (100,120,140,160,180),
                                    (100,100,140,160), (120,100,140,160),
                                    (0,100,120,140), (True,100,120,140),
                                    (90.5,100,120,140), ('90',100,120,140)])
def test_invalid_boundaries_are_rejected(values):
    with pytest.raises(ValueError):
        HeartRateZones(values)
    with pytest.raises(ValueError):
        Config(hr_zone_boundaries=list(values)).validate()


@pytest.mark.parametrize('values', [None, '100,120,140,160', 123])
def test_config_rejects_non_list_zone_config(values):
    with pytest.raises(ValueError):
        Config(hr_zone_boundaries=values).validate()
